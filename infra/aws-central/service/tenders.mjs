const STOP = new Set([
  "road", "roads", "street", "cross", "main", "layout", "bengaluru", "bangalore",
  "karnataka", "india", "ward", "city", "corporation", "south", "north", "east",
  "west", "central", "urban", "sector", "stage", "block", "phase", "nagar",
  "area", "locality", "village", "town", "zone", "division", "circle", "junction",
]);
const SURFACE_WORK = [
  /\b(?:pothole|pot\s*hole)s?\b.{0,80}\b(?:fill\w*|patch\w*|repair\w*|maint\w*)\b/i,
  /\b(?:fill\w*|patch\w*|repair\w*|maint\w*)\b.{0,80}\b(?:pothole|pot\s*hole)s?\b/i,
  /\b(?:resurfac\w*|re-?asphalt\w*|asphalt\w*|blacktopp?\w*|concret\w*|widen\w*|strengthen\w*|rehabilitat\w*)\b.{0,160}\b(?:road|roads|carriageway|pavement)\b/i,
  /\b(?:road|roads|carriageway|pavement)\b.{0,100}\b(?:repair\w*|maint\w*|resurfac\w*|rehabilitat\w*|reconstruct\w*)\b/i,
];
const NON_SURFACE = /\b(?:footpaths?|sidewalks?|walkways?|kerbs?|curbs?|drains?|drainage|culverts?|utilities|landscap\w*|buildings?|parks?|medians?|lighting|signage)\b/i;

function tokens(value) {
  const words = String(value || "").toLowerCase().split(/[^a-z0-9]+/).filter(Boolean);
  const result = new Set(words.filter((word) => word.length > 2 && !STOP.has(word)));
  for (let index = 0; index + 1 < words.length; index += 1) {
    const joined = words[index] + words[index + 1];
    if (joined.length >= 5 && (!STOP.has(words[index]) || !STOP.has(words[index + 1]))) {
      result.add(joined);
    }
  }
  return result;
}

export function hasRoadSurfaceScope(title) {
  const value = String(title || "");
  if (SURFACE_WORK.some((pattern) => pattern.test(value))) return true;
  if (NON_SURFACE.test(value)) return false;
  return /\b(?:repair|maintenance|improvement|development|construction)\w*\b.{0,100}\broads?\b/i
    .test(value);
}

function publicTender(tender, confidence, reason) {
  return {
    tender_number: tender.tender_number,
    title: tender.title,
    location: tender.location || null,
    contractor: tender.contractor || null,
    published: tender.published || null,
    confidence,
    reason,
    match_method: "deterministic_location_scope",
    warranty: "current liability not established by the publication record",
    warranty_code: "unverified",
    source_name: tender.source_name || null,
    source_url: tender.source_url || null,
  };
}

export function matchTender(address, tenders) {
  const wanted = tokens(String(address || "").split(",").slice(0, 4).join(","));
  if (!wanted.size) return { tender: null, reason: "address_unresolved" };
  const eligible = [];
  for (const tender of tenders) {
    if (!hasRoadSurfaceScope(tender.title)) continue;
    const candidate = tokens(`${tender.title || ""} ${tender.location || ""}`);
    let overlap = 0;
    for (const token of wanted) if (candidate.has(token)) overlap += 1;
    if (overlap) eligible.push({ tender, overlap });
  }
  if (!eligible.length) return { tender: null, reason: "no_location_match" };
  eligible.sort((left, right) => right.overlap - left.overlap
    || String(left.tender.tender_number).localeCompare(String(right.tender.tender_number)));
  if (eligible[1] && eligible[1].overlap === eligible[0].overlap) {
    return { tender: null, reason: "no_confident_match" };
  }
  const confidence = Math.min(0.95, 0.62 + eligible[0].overlap * 0.08);
  return {
    tender: publicTender(
      eligible[0].tender,
      confidence,
      `${eligible[0].overlap} location token(s) matched an explicit road-surface scope.`,
    ),
    reason: null,
  };
}
