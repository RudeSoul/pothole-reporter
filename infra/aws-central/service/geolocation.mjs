import { HttpError } from "./errors.mjs";

const KGIS_TOWN = "https://kgis.ksrsac.in/kgismaps/rest/services/Boundaries/Admin_Dynamic_New/MapServer/1/query";
const KGIS_NH = "https://kgis.ksrsac.in/kgismaps/rest/services/State_Basemap/State_Basemap_Dynamic/MapServer/289/query";
const KGIS_SH = "https://kgis.ksrsac.in/kgismaps/rest/services/State_Basemap/State_Basemap_Dynamic/MapServer/290/query";
const KGIS_DH = "https://kgis.ksrsac.in/kgismaps/rest/services/State_Basemap/State_Basemap_Dynamic/MapServer/291/query";
const KGIS_GP = "https://kgis.ksrsac.in/kgismaps/rest/services/Boundaries/GP_Boundary/MapServer/0/query";

const bounded = (value, maximum) => typeof value === "string"
  ? value.trim().slice(0, maximum) : "";

function pointUrl(endpoint, lat, lng, fields, distance = 0) {
  const geometry = encodeURIComponent(JSON.stringify({
    x: lng,
    y: lat,
    spatialReference: { wkid: 4326 },
  }));
  return `${endpoint}?geometry=${geometry}`
    + "&geometryType=esriGeometryPoint&spatialRel=esriSpatialRelIntersects"
    + (distance ? `&distance=${distance}&units=esriSRUnit_Meter` : "")
    + `&outFields=${encodeURIComponent(fields)}&returnGeometry=false&f=json`;
}

async function readJson(fetchImpl, url, { headers = {}, timeoutMs = 6_000 } = {}) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetchImpl(url, {
      headers,
      redirect: "error",
      signal: controller.signal,
    });
    if (!response.ok) return { available: false, data: null };
    const data = await response.json();
    if (!data || typeof data !== "object" || data.error) {
      return { available: false, data: null };
    }
    return { available: true, data };
  } catch {
    return { available: false, data: null };
  } finally {
    clearTimeout(timer);
  }
}

function addressFromGeocoder(data) {
  const address = data?.address || {};
  const parts = [
    address.road || address.pedestrian || address.residential || address.footway,
    address.neighbourhood || address.hamlet,
    address.suburb || address.village,
    address.city || address.town || address.municipality,
    address.postcode,
  ].filter((value, index, all) => value && all.indexOf(value) === index);
  return bounded(parts.join(", ") || data?.display_name, 500) || null;
}

export function createGeolocator({
  fetchImpl = fetch,
  geocoderUrl = "",
  geocoderBearerToken = "",
  highwayProximityMetres = 20,
} = {}) {
  const cache = new Map();
  return {
    async resolve({ lat, lng, addressHint = "" }) {
      const cacheKey = `${lat.toFixed(5)},${lng.toFixed(5)}`;
      const cached = cache.get(cacheKey);
      if (cached && cached.expiresAt > Date.now()) {
        return { ...cached.value, address: cached.value.address || bounded(addressHint, 500) || null };
      }
      const proximity = Math.min(50, Math.max(5, Number(highwayProximityMetres) || 20));
      let geocoder = null;
      if (geocoderUrl) {
        try {
          const url = new URL(geocoderUrl);
          if (url.protocol !== "https:" || url.username || url.password) throw new Error();
          url.searchParams.set("lat", String(lat));
          url.searchParams.set("lon", String(lng));
          url.searchParams.set("format", "jsonv2");
          url.searchParams.set("zoom", "17");
          url.searchParams.set("addressdetails", "1");
          geocoder = {
            url: url.href,
            headers: geocoderBearerToken
              ? { authorization: `Bearer ${geocoderBearerToken}` } : {},
          };
        } catch {
          throw new HttpError(503, "geocoder_misconfigured",
            "The operator geocoder URL is invalid.");
        }
      }
      const [town, nh, sh, dh, geocoded] = await Promise.all([
        readJson(fetchImpl, pointUrl(KGIS_TOWN, lat, lng,
          "KGISTownName,Town_Type,KGISTownCode,LGD_TownCode")),
        readJson(fetchImpl, pointUrl(KGIS_NH, lat, lng, "Name", proximity)),
        readJson(fetchImpl, pointUrl(KGIS_SH, lat, lng, "Name", proximity)),
        readJson(fetchImpl, pointUrl(KGIS_DH, lat, lng, "Name", proximity)),
        geocoder
          ? readJson(fetchImpl, geocoder.url, { headers: geocoder.headers })
          : Promise.resolve({ available: false, data: null }),
      ]);
      const highwayLayers = [
        [nh, "national_highway"],
        [sh, "state_highway"],
        [dh, "district_highway"],
      ];
      const kgisAvailable = town.available && highwayLayers.every(([item]) => item.available);
      const townFeature = town.data?.features?.[0];
      const highway = highwayLayers.find(([item]) => item.data?.features?.[0]);
      const attrs = townFeature?.attributes || {};
      const lgd = attrs.LGD_TownCode == null ? "" : bounded(String(attrs.LGD_TownCode), 64);
      let roadOwnership = "unknown";
      let highwayName = null;
      let ruralBody = null;
      let gpAvailable = false;
      if (kgisAvailable) {
        if (highway) {
          roadOwnership = highway[1];
          highwayName = bounded(highway[0].data.features[0]?.attributes?.Name, 160) || null;
        } else if (townFeature && lgd) {
          roadOwnership = "municipal";
        } else if (!townFeature) {
          const gp = await readJson(fetchImpl, pointUrl(KGIS_GP, lat, lng, "KGISGPName"));
          gpAvailable = gp.available;
          ruralBody = bounded(gp.data?.features?.[0]?.attributes?.KGISGPName, 160) || null;
          roadOwnership = gp.available ? (ruralBody ? "rural" : "outside_state") : "unknown";
        }
      }
      const municipal = roadOwnership === "municipal";
      const value = {
        lat,
        lng,
        address: addressFromGeocoder(geocoded.data) || bounded(addressHint, 500) || null,
        lgd: municipal ? lgd || null : null,
        town: municipal ? bounded(attrs.KGISTownName, 160) || null : null,
        source: municipal && lgd ? "kgis" : "unresolved",
        address_source: geocoded.available
          ? "operator_geocoder" : addressHint ? "client_hint" : "unresolved",
        road_ownership: roadOwnership,
        highway_name: highwayName,
        rural_body: ruralBody,
        lookup: {
          kgis: kgisAvailable ? "available" : "unavailable",
          kgis_town: town.available ? "available" : "unavailable",
          kgis_highway: highwayLayers.every(([item]) => item.available)
            ? "available" : "unavailable",
          kgis_gp: gpAvailable ? "available" : "not_needed_or_unavailable",
          geocoder: geocoded.available ? "available"
            : addressHint ? "skipped_client_hint" : "unavailable",
        },
      };
      if (roadOwnership !== "unknown" && (!municipal || value.address)) {
        cache.set(cacheKey, { value, expiresAt: Date.now() + 300_000 });
        while (cache.size > 256) cache.delete(cache.keys().next().value);
      }
      return value;
    },
  };
}
