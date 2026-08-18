import fs from "node:fs/promises";
import { spawn } from "node:child_process";
import path from "node:path";
import { pathToFileURL } from "node:url";

const TITLE_FILL = "#17365D";
const HEADER_FILL = "#1F4E78";
const LABEL_FILL = "#D9EAF7";
const LIGHT_BORDER = "#D9E2F3";
const RENDERER_CLEANUP_EXIT_CODE = 3221226505;
const MAX_WORKER_LOG_CHARACTERS = 8192;
const MAX_PREVIEW_ROWS = 100;
const PNG_SIGNATURE = Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]);
const PERCENT_HEADERS = new Set([
  "ctr",
  "engagementrate",
  "gscctr",
  "gscctrcurrent",
  "gscctrprevious",
  "gscctrdelta",
]);
const COUNT_HEADERS = new Set([
  "sessions",
  "totalusers",
  "activeusers",
  "engagedsessions",
  "screenpageviews",
  "keyevents",
  "clicks",
  "impressions",
  "rows",
  "currentcanonicalpages",
  "ga4sessions",
  "ga4sessionscurrent",
  "ga4sessionsprevious",
  "ga4sessionsdelta",
  "gscclicks",
  "gscclickscurrent",
  "gscclicksprevious",
  "gscclicksdelta",
  "gscimpressions",
  "gscimpressionscurrent",
  "gscimpressionsprevious",
  "gscimpressionsdelta",
]);
const DECIMAL_HEADERS = new Set(["position"]);
const FIXED_SHEET_NAMES = new Set([
  "README",
  "Executive Summary",
  "Product Weekly Summary",
  "Product Page Mapping",
  "GA4 Daily",
  "GA4 Pages",
  "GSC Daily",
  "GSC Pages",
  "GSC Queries",
  "Audit",
]);

if (isDirectInvocation()) {
  const options = parseArgs(process.argv.slice(2));
  const result = options.worker
    ? await buildWorkbook(options)
    : await superviseRenderer(options);
  process.stdout.write(`${JSON.stringify(result, null, 2)}\n`);
}

async function buildWorkbook(workerOptions) {
  const { SpreadsheetFile, Workbook } = await import("@oai/artifact-tool");
  const payload = JSON.parse(await fs.readFile(workerOptions.input, "utf8"));
  validatePayload(payload);
  const workbook = Workbook.create();
  const renderedSheets = [];

  for (const sheetData of payload.sheets) {
    const sheet = workbook.worksheets.add(sheetData.name);
    const rows = normalizeRows(sheetData.rows);
    const columnCount = rows[0].length;
    sheet.showGridLines = false;
    sheet.getRangeByIndexes(0, 0, rows.length, columnCount).values = rows;
    formatSheet(sheet, sheetData, rows);

    const inspection = await workbook.inspect({
      kind: "table",
      range: `${sheetData.name}!A1:${columnLetter(columnCount)}${Math.min(rows.length, 20)}`,
      include: "values,formulas",
      tableMaxRows: 20,
      tableMaxCols: Math.min(columnCount, 12),
      maxChars: 2000,
    });
    if (!inspection.ndjson) {
      throw new Error(`Could not inspect sheet ${sheetData.name}`);
    }

    const previewRange = `A1:${columnLetter(columnCount)}${Math.min(rows.length, MAX_PREVIEW_ROWS)}`;
    const preview = await workbook.render({
      sheetName: sheetData.name,
      range: previewRange,
      autoCrop: "all",
      scale: 1,
      format: "png",
    });
    await fs.mkdir(workerOptions.renderDir, { recursive: true });
    const renderPath = path.join(workerOptions.renderDir, `${slugify(sheetData.name)}.png`);
    await fs.writeFile(renderPath, new Uint8Array(await preview.arrayBuffer()));
    renderedSheets.push(renderPath);
  }

  const formulaErrors = await workbook.inspect({
    kind: "match",
    searchTerm: "#REF!|#DIV/0!|#VALUE!|#NAME\\?|#N/A",
    options: { useRegex: true, maxResults: 300 },
    summary: "final formula error scan",
    maxChars: 2000,
  });
  if (hasFormulaErrors(formulaErrors.ndjson)) {
    throw new Error("Workbook contains formula errors");
  }

  await fs.mkdir(path.dirname(workerOptions.output), { recursive: true });
  const output = await SpreadsheetFile.exportXlsx(workbook);
  await output.save(workerOptions.output);
  return { output: workerOptions.output, renderedSheets };
}

export async function superviseRenderer(
  supervisorOptions,
  { workerRunner = runRendererWorker, move = fs.rename } = {},
) {
  const payload = JSON.parse(await fs.readFile(supervisorOptions.input, "utf8"));
  validatePayload(payload);
  const finalPaths = await validateFinalPaths(supervisorOptions, payload);
  const staging = await createStagingPaths(finalPaths);
  try {
    const worker = await workerRunner({
      ...supervisorOptions,
      output: staging.output,
      renderDir: staging.renderDir,
    });
    const stagedRenders = payload.sheets.map((sheetData) =>
      path.join(staging.renderDir, `${slugify(sheetData.name)}.png`),
    );
    const verified = await verifyGeneratedArtifacts(staging.output, stagedRenders);
    const expectedCleanupFault =
      worker.exitCode === RENDERER_CLEANUP_EXIT_CODE && worker.signal === null;
    if (!verified || (worker.exitCode !== 0 && !expectedCleanupFault)) {
      throw new Error(formatWorkerFailure(worker, verified));
    }

    await promoteGeneratedArtifacts(staging, finalPaths, move);
    if (expectedCleanupFault) {
      process.stderr.write(
        `Artifact Tool renderer worker exited ${worker.exitCode} after staged outputs were verified; this is the documented renderer cleanup fault.\n`,
      );
    }
    return {
      output: finalPaths.output,
      renderedSheets: finalPaths.renderPaths,
      workerExitCode: worker.exitCode,
      outputsVerified: true,
    };
  } finally {
    await fs.rm(staging.outputDirectory, { recursive: true, force: true });
    await fs.rm(staging.renderDir, { recursive: true, force: true });
  }
}

async function validateFinalPaths(supervisorOptions, payload) {
  const input = path.resolve(supervisorOptions.input);
  const output = path.resolve(supervisorOptions.output);
  const renderDir = path.resolve(supervisorOptions.renderDir);
  const workingRoot = path.resolve(process.cwd());
  if (path.extname(output).toLowerCase() !== ".xlsx") {
    throw new Error("--output must be an absent or non-symlink regular .xlsx file");
  }
  if (output === input || output === renderDir || output === workingRoot) {
    throw new Error("--output must not target the input, render directory, or working root");
  }
  if (renderDir === input || renderDir === output || renderDir === workingRoot) {
    throw new Error("--render-dir must not target the input, output, or working root");
  }
  if (isPathWithin(renderDir, output)) {
    throw new Error("--output must not be inside --render-dir");
  }
  await assertFileTarget(output, "--output must be an absent or non-symlink regular .xlsx file");
  await assertDirectoryTarget(
    renderDir,
    "--render-dir must be an absent or non-symlink directory",
  );

  const renderPaths = payload.sheets.map((sheetData) =>
    path.resolve(renderDir, `${slugify(sheetData.name)}.png`),
  );
  for (const renderPath of renderPaths) {
    if (!isPathWithin(renderDir, renderPath)) {
      throw new Error("expected render path must stay within --render-dir");
    }
    await assertFileTarget(
      renderPath,
      "expected render file must be absent or a non-symlink regular file",
    );
  }
  return {
    input,
    output,
    renderDir,
    renderPaths,
    renderDirExisted: await pathExists(renderDir),
  };
}

async function assertFileTarget(target, message) {
  const status = await lstatOrNull(target);
  if (status && (status.isSymbolicLink() || !status.isFile())) throw new Error(message);
}

async function assertDirectoryTarget(target, message) {
  const status = await lstatOrNull(target);
  if (status && (status.isSymbolicLink() || !status.isDirectory())) throw new Error(message);
}

async function lstatOrNull(target) {
  try {
    return await fs.lstat(target);
  } catch (error) {
    if (error?.code === "ENOENT") return null;
    throw error;
  }
}

async function pathExists(target) {
  return (await lstatOrNull(target)) !== null;
}

function isPathWithin(parent, candidate) {
  const relative = path.relative(parent, candidate);
  return relative !== "" && !relative.startsWith(`..${path.sep}`) && !path.isAbsolute(relative);
}

async function createStagingPaths(finalPaths) {
  await fs.mkdir(path.dirname(finalPaths.output), { recursive: true });
  await fs.mkdir(path.dirname(finalPaths.renderDir), { recursive: true });
  const outputDirectory = await fs.mkdtemp(
    path.join(path.dirname(finalPaths.output), `.${path.basename(finalPaths.output)}.staging-`),
  );
  const renderDir = await fs.mkdtemp(
    path.join(path.dirname(finalPaths.renderDir), `.${path.basename(finalPaths.renderDir)}.staging-`),
  );
  return {
    outputDirectory,
    output: path.join(outputDirectory, path.basename(finalPaths.output)),
    renderDir,
  };
}

async function promoteGeneratedArtifacts(staging, finalPaths, move) {
  const artifacts = [
    { staged: staging.output, final: finalPaths.output },
    ...finalPaths.renderPaths.map((final) => ({
      staged: path.join(staging.renderDir, path.basename(final)),
      final,
    })),
  ];
  const backups = [];
  const promoted = [];
  let renderDirectoryCreated = false;
  try {
    for (const artifact of artifacts) {
      backups.push(await moveExistingPathToBackup(artifact.final, move));
    }
    if (!finalPaths.renderDirExisted) {
      await fs.mkdir(finalPaths.renderDir);
      renderDirectoryCreated = true;
    }
    for (const artifact of artifacts) {
      await move(artifact.staged, artifact.final);
      promoted.push(artifact);
    }
  } catch (error) {
    try {
      await rollbackPromotion(promoted, backups, finalPaths, renderDirectoryCreated, move);
    } catch (rollbackError) {
      throw new Error(`${error.message}\n${rollbackError.message}`);
    }
    throw error;
  }
  await discardBackups(backups, "committed");
}

async function moveExistingPathToBackup(finalPath, move) {
  const status = await lstatOrNull(finalPath);
  if (!status) return null;
  const directory = await fs.mkdtemp(
    path.join(path.dirname(finalPath), `.${path.basename(finalPath)}.previous-`),
  );
  const backupPath = path.join(directory, path.basename(finalPath));
  await move(finalPath, backupPath);
  return { directory, path: backupPath, final: finalPath };
}

async function rollbackPromotion(promoted, backups, finalPaths, renderDirectoryCreated, move) {
  const rollbackErrors = [];
  const recoveryPaths = [];
  for (const artifact of [...promoted].reverse()) {
    try {
      await fs.rm(artifact.final, { force: true });
    } catch (error) {
      rollbackErrors.push(error);
    }
  }
  for (const backup of [...backups].reverse()) {
    if (!backup) continue;
    try {
      await move(backup.path, backup.final);
      await discardBackups([backup], "rolled back");
    } catch (error) {
      rollbackErrors.push(error);
      recoveryPaths.push(backup.path);
      process.stderr.write(
        `Rollback restore failed; original artifact remains recoverable at ${backup.path}: ${error.message}\n`,
      );
    }
  }
  if (renderDirectoryCreated) {
    try {
      await fs.rmdir(finalPaths.renderDir);
    } catch (error) {
      if (error?.code !== "ENOENT" && error?.code !== "ENOTEMPTY") rollbackErrors.push(error);
    }
  }
  if (rollbackErrors.length > 0) {
    throw new Error(
      `Promotion rollback failed: ${rollbackErrors.map((error) => error.message).join("; ")}\n` +
      `Recovery backups retained: ${recoveryPaths.join(", ")}`,
    );
  }
}

async function discardBackups(backups, outcome) {
  for (const backup of backups) {
    if (!backup) continue;
    try {
      await fs.rm(backup.directory, { recursive: true, force: true });
    } catch (error) {
      process.stderr.write(
        `Promotion ${outcome}, but recoverable backup remains at ${backup.directory}: ${error.message}\n`,
      );
    }
  }
}

function runRendererWorker(supervisorOptions) {
  return new Promise((resolve, reject) => {
    const child = spawn(
      process.execPath,
      [
        process.argv[1],
        "--worker",
        "--input",
        supervisorOptions.input,
        "--output",
        supervisorOptions.output,
        "--render-dir",
        supervisorOptions.renderDir,
      ],
      { stdio: ["ignore", "pipe", "pipe"] },
    );
    let stdout = "";
    let stderr = "";
    child.stdout.setEncoding("utf8");
    child.stderr.setEncoding("utf8");
    child.stdout.on("data", (chunk) => {
      stdout = appendBoundedLog(stdout, chunk);
    });
    child.stderr.on("data", (chunk) => {
      stderr = appendBoundedLog(stderr, chunk);
    });
    child.on("error", reject);
    child.on("close", (exitCode, signal) => resolve({ exitCode, signal, stdout, stderr }));
  });
}

function appendBoundedLog(existing, chunk) {
  if (existing.length >= MAX_WORKER_LOG_CHARACTERS) return existing;
  const remaining = MAX_WORKER_LOG_CHARACTERS - existing.length;
  const text = String(chunk);
  return text.length <= remaining
    ? existing + text
    : `${existing + text.slice(0, remaining)}\n[worker log truncated]`;
}

function formatWorkerFailure(worker, outputsVerified) {
  const outcome = worker.signal ? `signal ${worker.signal}` : `exit ${worker.exitCode}`;
  const stdout = worker.stdout?.trim() || "<empty>";
  const stderr = worker.stderr?.trim() || "<empty>";
  return [
    `Artifact Tool renderer failed with ${outcome}; staged outputs verified: ${outputsVerified}.`,
    `worker stdout: ${stdout}`,
    `worker stderr: ${stderr}`,
  ].join("\n");
}

export async function verifyGeneratedArtifacts(outputPath, renderPaths) {
  try {
    const output = await fs.readFile(outputPath);
    if (!isValidXlsxArchive(output)) return false;
    await Promise.all(
      renderPaths.map(async (renderPath) => {
        const render = await fs.readFile(renderPath);
        if (!hasPngSignature(render)) {
          throw new Error(`Missing render ${renderPath}`);
        }
      }),
    );
    return true;
  } catch {
    return false;
  }
}

function hasPngSignature(content) {
  return content.length >= PNG_SIGNATURE.length && content.subarray(0, PNG_SIGNATURE.length).equals(PNG_SIGNATURE);
}

function isDirectInvocation() {
  const scriptPath = process.argv[1];
  return Boolean(scriptPath) && import.meta.url === pathToFileURL(scriptPath).href;
}

function isValidXlsxArchive(content) {
  const localFileHeader = 0x04034b50;
  const centralDirectoryHeader = 0x02014b50;
  const endOfCentralDirectory = 0x06054b50;
  if (content.length < 22 || content.readUInt32LE(0) !== localFileHeader) return false;

  const eocdOffset = findEndOfCentralDirectory(content, endOfCentralDirectory);
  if (eocdOffset < 0 || eocdOffset + 22 > content.length) return false;
  const commentLength = content.readUInt16LE(eocdOffset + 20);
  if (eocdOffset + 22 + commentLength !== content.length) return false;
  const diskNumber = content.readUInt16LE(eocdOffset + 4);
  const centralDirectoryDisk = content.readUInt16LE(eocdOffset + 6);
  const entriesOnDisk = content.readUInt16LE(eocdOffset + 8);
  const totalEntries = content.readUInt16LE(eocdOffset + 10);
  const centralDirectorySize = content.readUInt32LE(eocdOffset + 12);
  const centralDirectoryOffset = content.readUInt32LE(eocdOffset + 16);
  if (
    diskNumber !== 0 ||
    centralDirectoryDisk !== 0 ||
    entriesOnDisk !== totalEntries ||
    totalEntries === 0 ||
    centralDirectoryOffset + centralDirectorySize > eocdOffset
  ) {
    return false;
  }

  const names = new Set();
  let offset = centralDirectoryOffset;
  for (let index = 0; index < totalEntries; index += 1) {
    if (offset + 46 > eocdOffset || content.readUInt32LE(offset) !== centralDirectoryHeader) {
      return false;
    }
    const nameLength = content.readUInt16LE(offset + 28);
    const extraLength = content.readUInt16LE(offset + 30);
    const entryCommentLength = content.readUInt16LE(offset + 32);
    const entryEnd = offset + 46 + nameLength + extraLength + entryCommentLength;
    if (entryEnd > eocdOffset) return false;
    names.add(content.subarray(offset + 46, offset + 46 + nameLength).toString("utf8"));
    offset = entryEnd;
  }
  return (
    offset === centralDirectoryOffset + centralDirectorySize &&
    names.has("[Content_Types].xml") &&
    names.has("xl/workbook.xml") &&
    names.has("xl/_rels/workbook.xml.rels")
  );
}

function findEndOfCentralDirectory(content, signature) {
  const start = Math.max(0, content.length - 0xffff - 22);
  for (let offset = content.length - 22; offset >= start; offset -= 1) {
    if (content.readUInt32LE(offset) === signature) return offset;
  }
  return -1;
}

function parseArgs(args) {
  const values = {};
  for (let index = 0; index < args.length; ) {
    const flag = args[index];
    if (flag === "--worker") {
      if (values.worker) throw new Error("--worker may be specified only once");
      values.worker = true;
      index += 1;
      continue;
    }
    const value = args[index + 1];
    if (!flag?.startsWith("--") || value === undefined) {
      throw new Error("Expected --input, --output, and --render-dir arguments");
    }
    const key = flag.slice(2).replace(/-([a-z])/g, (_, letter) => letter.toUpperCase());
    if (!(key in { input: true, output: true, renderDir: true }) || values[key]) {
      throw new Error("Expected each of --input, --output, and --render-dir exactly once");
    }
    values[key] = value;
    index += 2;
  }
  const expectedCount = values.worker ? 4 : 3;
  if (
    !values.input ||
    !values.output ||
    !values.renderDir ||
    Object.keys(values).length !== expectedCount
  ) {
    throw new Error("Expected --input, --output, and --render-dir arguments");
  }
  return values;
}

function validatePayload(payload) {
  if (!payload || !Array.isArray(payload.sheets) || payload.sheets.length === 0) {
    throw new Error("Workbook payload must contain non-empty sheets");
  }
  const names = new Set();
  for (const sheetData of payload.sheets) {
    if (
      !sheetData ||
      !FIXED_SHEET_NAMES.has(sheetData.name) ||
      names.has(sheetData.name) ||
      !Array.isArray(sheetData.rows) ||
      sheetData.rows.length === 0
    ) {
      throw new Error("Workbook payload contains an invalid sheet");
    }
    names.add(sheetData.name);
  }
}

function normalizeRows(rows) {
  const width = Math.max(...rows.map((row) => (Array.isArray(row) ? row.length : 0)));
  if (!width) {
    throw new Error("Workbook sheet rows must have at least one column");
  }
  const headers = rows[0];
  if (!Array.isArray(headers)) {
    throw new Error("Workbook sheet rows must be arrays");
  }
  return rows.map((row, rowIndex) => {
    if (!Array.isArray(row)) {
      throw new Error("Workbook sheet rows must be arrays");
    }
    return Array.from({ length: width }, (_, columnIndex) => {
      const value = row[columnIndex] ?? null;
      const header = typeof headers[columnIndex] === "string" ? headers[columnIndex] : "";
      return rowIndex > 0 && isDateHeader(header) && isIsoDate(value)
        ? new Date(`${value}T00:00:00Z`)
        : value;
    });
  });
}

function formatSheet(sheet, sheetData, rows) {
  const rowCount = rows.length;
  const columnCount = rows[0].length;
  const allRange = sheet.getRangeByIndexes(0, 0, rowCount, columnCount);
  allRange.format.wrapText = true;
  applyColumnWidths(sheet, rows);
  if (sheetData.kind === "readme") {
    allRange.format.autofitRows();
  }

  if (sheetData.detail) {
    styleHeader(sheet.getRangeByIndexes(0, 0, 1, columnCount));
    sheet.freezePanes.freezeRows(1);
    applyDetailNumberFormats(sheet, rows);
    return;
  }

  styleTitle(sheet.getRangeByIndexes(0, 0, 1, columnCount));
  if (columnCount > 1) {
    sheet.mergeCells(`A1:${columnLetter(columnCount)}1`);
  }
  const tableHeaderRow =
    sheetData.kind === "summary" || sheetData.kind === "product_summary" || sheetData.kind === "audit"
      ? findTableHeaderRow(rows)
      : -1;
  styleLabels(sheet, rows, sheetData.kind, tableHeaderRow);
  if (sheetData.kind === "summary") {
    styleHeader(sheet.getRangeByIndexes(tableHeaderRow, 0, 1, columnCount));
    applySummaryNumberFormats(sheet, rows, tableHeaderRow);
  }
  if (sheetData.kind === "product_summary") {
    styleHeader(sheet.getRangeByIndexes(tableHeaderRow, 0, 1, columnCount));
    applyProductSummaryNumberFormats(sheet, rows, tableHeaderRow);
  }
  if (sheetData.kind === "audit") {
    styleHeader(sheet.getRangeByIndexes(tableHeaderRow, 0, 1, columnCount));
    const auditDataRows = rowCount - tableHeaderRow - 1;
    if (auditDataRows > 0) {
      sheet.getRangeByIndexes(tableHeaderRow + 1, 2, auditDataRows, 1).format.numberFormat = "#,##0";
    }
  }
}

function styleTitle(range) {
  range.format = {
    fill: TITLE_FILL,
    font: { bold: true, color: "#FFFFFF", size: 16 },
    horizontalAlignment: "left",
    verticalAlignment: "center",
    wrapText: true,
    borders: { preset: "outside", style: "thin", color: TITLE_FILL },
  };
  range.format.rowHeight = 28;
}

function styleHeader(range) {
  range.format = {
    fill: HEADER_FILL,
    font: { bold: true, color: "#FFFFFF" },
    verticalAlignment: "center",
    wrapText: true,
    borders: { preset: "all", style: "thin", color: LIGHT_BORDER },
  };
  range.format.rowHeight = 22;
}

function styleLabels(sheet, rows, kind, tableHeaderRow) {
  const finalLabelRow = kind === "readme" ? rows.length : tableHeaderRow;
  for (let rowIndex = 1; rowIndex < finalLabelRow; rowIndex += 1) {
    if (rows[rowIndex].every((value) => value === null || value === "")) continue;
    const range = sheet.getRangeByIndexes(rowIndex, 0, 1, 1);
    range.format = {
      fill: LABEL_FILL,
      font: { bold: true, color: "#17365D" },
      borders: { preset: "outside", style: "thin", color: LIGHT_BORDER },
    };
  }
}

function applyDetailNumberFormats(sheet, rows) {
  const headers = rows[0];
  for (let index = 0; index < headers.length; index += 1) {
    const header = String(headers[index] ?? "").toLowerCase();
    const dataRange = sheet.getRangeByIndexes(1, index, Math.max(rows.length - 1, 0), 1);
    if (isDateHeader(header)) {
      dataRange.format.numberFormat = "yyyy-mm-dd";
    } else if (PERCENT_HEADERS.has(header)) {
      dataRange.format.numberFormat = "0.0%";
    } else if (COUNT_HEADERS.has(header)) {
      dataRange.format.numberFormat = "#,##0";
    } else if (DECIMAL_HEADERS.has(header)) {
      dataRange.format.numberFormat = "0.0";
    }
  }
}

function applySummaryNumberFormats(sheet, rows, tableHeaderRow) {
  const dataRows = Math.max(rows.length - tableHeaderRow - 1, 0);
  if (!dataRows) return;
  sheet.getRangeByIndexes(tableHeaderRow + 1, 2, dataRows, 3).format.numberFormat = "#,##0";
  for (let rowIndex = tableHeaderRow + 1; rowIndex < rows.length; rowIndex += 1) {
    const metric = String(rows[rowIndex][1] ?? "").toLowerCase();
    if (PERCENT_HEADERS.has(metric)) {
      sheet.getRangeByIndexes(rowIndex, 2, 1, 3).format.numberFormat = "0.0%";
    } else if (DECIMAL_HEADERS.has(metric)) {
      sheet.getRangeByIndexes(rowIndex, 2, 1, 3).format.numberFormat = "0.0";
    }
  }
}

function applyProductSummaryNumberFormats(sheet, rows, tableHeaderRow) {
  const headers = rows[tableHeaderRow];
  const dataRows = Math.max(rows.length - tableHeaderRow - 1, 0);
  for (let index = 0; index < headers.length; index += 1) {
    const header = String(headers[index] ?? "").toLowerCase();
    const dataRange = sheet.getRangeByIndexes(tableHeaderRow + 1, index, dataRows, 1);
    if (PERCENT_HEADERS.has(header)) {
      dataRange.format.numberFormat = "0.0%";
    } else if (COUNT_HEADERS.has(header)) {
      dataRange.format.numberFormat = "#,##0";
    }
  }
}

function findTableHeaderRow(rows) {
  const index = rows.findIndex(
    (row) =>
      (row[0] === "Source" && (row[1] === "Metric" || row[1] === "Status")) ||
      (row[0] === "reportLineId" && row[1] === "reportLine"),
  );
  if (index < 0) throw new Error("Summary and audit sheets require a supported table header");
  return index;
}

function applyColumnWidths(sheet, rows) {
  const columnCount = rows[0].length;
  for (let index = 0; index < columnCount; index += 1) {
    const length = Math.max(
      10,
      ...rows.map((row) => displayLength(row[index])),
    );
    sheet.getRangeByIndexes(0, index, rows.length, 1).format.columnWidth = Math.min(48, length + 2);
  }
}

function displayLength(value) {
  if (value === null || value === undefined) return 0;
  return String(value).length;
}

function isDateHeader(header) {
  return String(header).toLowerCase() === "date";
}

function isIsoDate(value) {
  return typeof value === "string" && /^\d{4}-\d{2}-\d{2}$/.test(value);
}

function columnLetter(index) {
  let remaining = index;
  let result = "";
  while (remaining > 0) {
    const modulo = (remaining - 1) % 26;
    result = String.fromCharCode(65 + modulo) + result;
    remaining = Math.floor((remaining - modulo) / 26);
  }
  return result;
}

function slugify(value) {
  return value.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/(^-|-$)/g, "");
}

function hasFormulaErrors(ndjson) {
  return /#REF!|#DIV\/0!|#VALUE!|#NAME\?|#N\/A/.test(ndjson ?? "");
}
