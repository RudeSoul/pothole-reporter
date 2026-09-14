const EARTH_RADIUS_M = 6_371_000;
const WEB_MERCATOR_RADIUS_M = 6_378_137;
const CELL_METRES = 30;

export function validLatLng(lat, lng) {
  return Number.isFinite(lat) && Number.isFinite(lng)
    && lat >= -85 && lat <= 85 && lng >= -180 && lng <= 180;
}

export function metresBetween(lat1, lng1, lat2, lng2) {
  const radians = Math.PI / 180;
  const p1 = lat1 * radians;
  const p2 = lat2 * radians;
  const dp = (lat2 - lat1) * radians;
  const dl = (lng2 - lng1) * radians;
  const a = Math.sin(dp / 2) ** 2
    + Math.cos(p1) * Math.cos(p2) * Math.sin(dl / 2) ** 2;
  return 2 * EARTH_RADIUS_M * Math.atan2(Math.sqrt(a), Math.sqrt(1 - a));
}

function projectedCell(lat, lng) {
  const radians = Math.PI / 180;
  const x = WEB_MERCATOR_RADIUS_M * lng * radians;
  const y = WEB_MERCATOR_RADIUS_M
    * Math.log(Math.tan(Math.PI / 4 + lat * radians / 2));
  return {
    x: Math.floor(x / CELL_METRES),
    y: Math.floor(y / CELL_METRES),
  };
}

export function spatialCell(lat, lng) {
  const cell = projectedCell(lat, lng);
  return `${cell.x}:${cell.y}`;
}

export function nearbyCells(lat, lng, radiusMetres) {
  const centre = projectedCell(lat, lng);
  const projectedRadius = radiusMetres / Math.max(0.5, Math.cos(lat * Math.PI / 180));
  const span = Math.min(4, Math.ceil(projectedRadius / CELL_METRES) + 1);
  const cells = [];
  for (let y = centre.y - span; y <= centre.y + span; y += 1) {
    for (let x = centre.x - span; x <= centre.x + span; x += 1) {
      cells.push(`${x}:${y}`);
    }
  }
  return cells;
}

export function roundedPublicCoordinate(value) {
  return Math.round(value * 100_000) / 100_000;
}
