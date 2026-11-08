// The sheet header's texts, from the sheet view and the sheet list. Pure
// functions: tested with node.

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

// a header's words as a key: "Substrate conductive layer" -> "substrate_conductive_layer"
export function fieldKey(name) {
  return String(name || "").toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_|_$/g, "");
}

// "20260914" (the sheet's yyyymmdd) or an ISO date -> "14 Sep 2026";
// anything else as it is
export function formatDay(value) {
  const text = String(value ?? "").trim();
  let m = /^(\d{4})(\d{2})(\d{2})$/.exec(text) || /^(\d{4})-(\d{2})-(\d{2})/.exec(text);
  if (!m) return text;
  const month = MONTHS[Number(m[2]) - 1];
  const day = Number(m[3]);
  if (!month || day < 1 || day > 31) return text;
  return day + " " + month + " " + m[1];
}

function withUnit(field) {
  return field.unit ? field.value + " " + field.unit : field.value;
}

// The header's line: what tells a batch from the others, the project
// name, the day and how many samples. The substrate bookkeeping (glass,
// size, pixels, areas, junctions, batch) is the same for months, so it is
// a line of the overview, not of the header.
export function metaLines(info) {
  // a blank cell is no value; two columns with the same words both show
  const all = (info.fields || []).filter((f) => f.value !== "");
  const used = new Set();
  const take = (key) => {
    const f = all.find((g) => !used.has(g) && fieldKey(g.name || g.header) === key);
    if (f) used.add(f);
    return f;
  };
  const main = [];
  const project = take("project_name");
  if (project) main.push(project.value);
  const date = take("date");
  if (date) main.push(formatDay(date.value));
  if (info.samples != null) {
    let samples = info.samples + (info.samples === 1 ? " sample" : " samples");
    const subbatches = info.subbatches || [];
    if (subbatches.length > 1) samples += " in " + subbatches.length + " subbatches";
    main.push(samples);
  }
  const substrate = [];
  const material = take("substrate_material");
  const layer = take("substrate_conductive_layer");
  if (material || layer) substrate.push([material, layer].filter(Boolean).map(withUnit).join(" / "));
  const dimension = take("sample_dimension");
  if (dimension) substrate.push(withUnit(dimension));
  const pixels = take("number_of_pixels");
  if (pixels) substrate.push(pixels.value + (pixels.value === "1" ? " pixel" : " pixels"));
  substrate.push(...all.filter((f) => !used.has(f)).map((f) => (f.name || f.header) + " " + withUnit(f)));
  return { main, substrate };
}

// "uploaded 2 Nov", with the year when it is not this one; "" without a time
export function uploadedText(created, now = new Date()) {
  if (!created) return "";
  const when = new Date(created);
  if (Number.isNaN(when.getTime())) return "";
  let text = "uploaded " + when.getDate() + " " + MONTHS[when.getMonth()];
  if (when.getFullYear() !== now.getFullYear()) text += " " + when.getFullYear();
  return text;
}

// the sheet's name as proposed from the uploaded file's: "Batch 7.xlsx" -> "Batch 7"
export function nameFromFile(fileName) {
  return String(fileName || "").replace(/^.*[\\/]/, "").replace(/\.[^.]*$/, "").trim();
}

export function isSheetFile(fileName) {
  return /\.xlsx$/i.test(String(fileName || ""));
}
