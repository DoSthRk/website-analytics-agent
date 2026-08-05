import fs from "node:fs/promises";
import { spawn } from "node:child_process";
import path from "node:path";

const TITLE_FILL = "#17365D";
const HEADER_FILL = "#1F4E78";
const LABEL_FILL = "#D9EAF7";
const LIGHT_BORDER = "#D9E2F3";
const PERCENT_HEADERS = new Set(["ctr", "engagementrate"]);
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
]);
const DECIMAL_HEADERS = new Set(["position"]);
const FIXED_SHEET_NAMES = new Set([
  "README",
  "Executive Summary",
  "GA4 Daily",
  "GA4 Pages",
  "GSC Daily",
  "GSC Pages",
  "GSC Queries",
  "Audit",
]);

const options = parseArgs(process.argv.slice(2));
const result = options.worker
  ? await buildWorkbook(options)
  : await superviseRenderer(options);
process.stdout.write(`${JSON.stringify(result, null, 2)}\n`);

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

    const preview = await workbook.render({
      sheetName: sheetData.name,
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

async function superviseRenderer(supervisorOptions) {
  const payload = JSON.parse(await fs.readFile(supervisorOptions.input, "utf8"));
  validatePayload(payload);
  const worker = await runRendererWorker(supervisorOptions);
  const renderedSheets = payload.sheets.map((sheetData) =>
    path.join(supervisorOptions.renderDir, `${slugify(sheetData.name)}.png`),
  );
  const verified = await verifyGeneratedArtifacts(supervisorOptions.output, renderedSheets);
  if (!verified) {
    throw new Error(
      `Artifact Tool renderer failed before producing all verified outputs (exit ${worker.exitCode ?? worker.signal ?? "unknown"}).`,
    );
  }
  if (worker.exitCode !== 0) {
    process.stderr.write(
      `Artifact Tool renderer worker exited ${worker.exitCode ?? worker.signal} after outputs were verified; this is an isolated renderer cleanup fault.\n`,
    );
  }
  return {
    output: supervisorOptions.output,
    renderedSheets,
    workerExitCode: worker.exitCode,
    outputsVerified: true,
  };
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
    child.on("error", reject);
    child.on("close", (exitCode, signal) => resolve({ exitCode, signal }));
  });
}

async function verifyGeneratedArtifacts(outputPath, renderPaths) {
  try {
    const output = await fs.readFile(outputPath);
    if (output.length < 4 || output[0] !== 0x50 || output[1] !== 0x4b) return false;
    await Promise.all(
      renderPaths.map(async (renderPath) => {
        const status = await fs.stat(renderPath);
        if (!status.isFile() || status.size === 0) {
          throw new Error(`Missing render ${renderPath}`);
        }
      }),
    );
    return true;
  } catch {
    return false;
  }
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
    sheetData.kind === "summary" || sheetData.kind === "audit"
      ? findTableHeaderRow(rows)
      : -1;
  styleLabels(sheet, rows, sheetData.kind, tableHeaderRow);
  if (sheetData.kind === "summary") {
    styleHeader(sheet.getRangeByIndexes(tableHeaderRow, 0, 1, columnCount));
    applySummaryNumberFormats(sheet, rows, tableHeaderRow);
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

function findTableHeaderRow(rows) {
  const index = rows.findIndex(
    (row) => row[0] === "Source" && (row[1] === "Metric" || row[1] === "Status"),
  );
  if (index < 0) throw new Error("Summary and audit sheets require a source table header");
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
