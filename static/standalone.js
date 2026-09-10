// The app engine. Reports and evidence stay in the browser database; the project service
// supplies shared vision, central tender/dedupe, aggregate impact, and the public map.
// The page's api() delegates every call here.
(() => {
  const NATIVE = !!(window.Capacitor && Capacitor.isNativePlatform && Capacitor.isNativePlatform());
  const LLM = window.PotholeLlmContract;
  if (!LLM || !LLM.prompts || !LLM.config) {
    throw new Error("The generated LLM contract must load before standalone.js.");
  }
  const DETECTION_PROMPT_CONFIG = LLM.prompts.detection;
  const TENDER_PROMPT_CONFIG = LLM.prompts.tender;
  const MODEL_CONFIG = LLM.config.models;
  const RUNTIME_CONFIG = LLM.config.runtime;
  const IMAGING_CONFIG = LLM.config.imaging;
  const TENDER_CONFIG = LLM.config.tender;

  // Browser-only development shortcut. A URL fragment never reaches HTTP access logs;
  // remove it immediately after seeding the local key. This never runs in the APK.
  if (!NATIVE) {
    const k = new URLSearchParams(location.hash.replace(/^#/, "")).get("key");
    if (k) {
      localStorage.setItem("openai_key", k);
      history.replaceState(null, "", location.pathname + location.search);
    }
  }

  // Shared detection is the zero-setup path. A saved Personal choice is effective only
  // while it has a key; this also repairs upgraded installations whose old migration
  // selected Personal even though no key was ever supplied. An explicit Shared choice
  // remains Shared even when a key is stored.
  function effectiveVisionProvider(selected, key) {
    const normalized = selected === "shared_server" ? "shared"
      : ["personal_openai", "own_key"].includes(selected) ? "personal" : selected;
    const hasKey = Boolean(String(key || "").trim());
    if (normalized === "shared") return "shared";
    if (normalized === "personal") return hasKey ? "personal" : "shared";
    return hasKey ? "personal" : "shared";
  }

  const S = {
    get key() { return (localStorage.getItem("openai_key") || "").trim(); },
    get provider() { return effectiveVisionProvider(localStorage.getItem("vision_provider"), this.key); },
    get name() { return (localStorage.getItem("sender_name") || "").trim() || "A concerned citizen"; },
    get debug() { return localStorage.getItem("debug_mode") === "1"; },
    get model() { return normaliseModel(localStorage.getItem("detection_model")); },
    get detail() { return normaliseDetail(localStorage.getItem("image_detail"), this.model); },
  };

  const LANG = () => MODEL_CONFIG.allowedLanguages.includes(localStorage.getItem("app_lang"))
    ? localStorage.getItem("app_lang") : MODEL_CONFIG.defaultLanguage;
  const PROGRESS = {
    en: { compress: "Preparing photo...", capture: "Preparing photo...",
          detect: "AI checking for road damage...", finalize: "Finalizing address and contract...",
          write: "Writing the complaint...", email: "Opening your email app..." },
    kn: { compress: "ಫೋಟೋ ಸಂಕುಚಿಸಲಾಗುತ್ತಿದೆ...", capture: "ಫ್ರೇಮ್ ಸೆರೆಹಿಡಿಯಲಾಗುತ್ತಿದೆ...",
          detect: "AI ರಸ್ತೆ ಹಾನಿ ಪರಿಶೀಲಿಸುತ್ತಿದೆ...", finalize: "ವಿಳಾಸ ಮತ್ತು ಗುತ್ತಿಗೆ ಖಚಿತಪಡಿಸಲಾಗುತ್ತಿದೆ...",
          write: "ದೂರು ಬರೆಯಲಾಗುತ್ತಿದೆ...", email: "ನಿಮ್ಮ ಇಮೇಲ್ ಆ್ಯಪ್ ತೆರೆಯಲಾಗುತ್ತಿದೆ..." },
  };
  const pmsg = (k) => (PROGRESS[LANG()] && PROGRESS[LANG()][k]) || PROGRESS.en[k];

  const DEFAULT_MODEL = MODEL_CONFIG.defaultModel;
  const ALLOWED_MODELS = new Set(MODEL_CONFIG.allowedModels);
  const ALLOWED_DETAILS = new Set(MODEL_CONFIG.allowedImageDetails);
  const ORIGINAL_DETAIL_MODELS = new Set(MODEL_CONFIG.originalDetailModels);
  const PROMPT_VERSION = DETECTION_PROMPT_CONFIG.version;
  const SCHEMA_VERSION = DETECTION_PROMPT_CONFIG.schemaVersion;
  const MAX_DETECTION_IMAGES = IMAGING_CONFIG.maxDetectionImages;
  // Detection still examines every burst. Only after a burst is accepted do we group it
  // with a road-damage event already saved at the same place. This preserves capture
  // recall while stopping adjacent bursts and later drives from creating repeat drafts.
  const DEDUPE_ADJACENT_RADIUS_M = 12;
  const DEDUPE_HISTORY_RADIUS_M = 8;
  const DEDUPE_MISSING_HEADING_RADIUS_M = 5;
  const DEDUPE_SAME_DRIVE_S = 4;
  const DEDUPE_POOR_GPS_S = 2;
  const DEDUPE_HISTORY_S = 30 * 24 * 60 * 60;
  const ACCEPTED_REPORT_STATUSES = new Set(["draft", "queued", "sent", "unrouted", "duplicate"]);
  const SERVICE_URL = (localStorage.getItem("service_url")
    || "https://pothole-detect.gauravsen.workers.dev").replace(/\/+$/, "");
  const usingSharedVision = () => S.provider === "shared";
  const INSTALLATION_KEY = "central_installation";

  function normaliseModel(value) {
    return ALLOWED_MODELS.has(value) ? value : DEFAULT_MODEL;
  }
  function normaliseDetail(value, model) {
    const picked = ALLOWED_DETAILS.has(value) ? value : MODEL_CONFIG.defaultImageDetail;
    // `original` is intentionally an experiment arm for the newest model. Older
    // vision models do not support it, so fail safely to their highest valid setting.
    return picked === MODEL_CONFIG.originalImageDetail && !ORIGINAL_DETAIL_MODELS.has(model)
      ? MODEL_CONFIG.defaultImageDetail : picked;
  }

  const OFFICERS = {
    "bengaluru central city corporation": ["Commissioner, Bengaluru Central City Corporation (BCCC)", "commissionerbccc@gmail.com"],
    "bengaluru east city corporation": ["Commissioner, Bengaluru East City Corporation (BECC)", "commissioner.becc@gmail.com"],
    "bengaluru north city corporation": ["Commissioner, Bengaluru North City Corporation (BNCC)", "bengalurunorthcitycorporation@gmail.com"],
    "bengaluru south city corporation": ["Commissioner, Bengaluru South City Corporation (BSCC)", "comm.south.gba@gmail.com"],
    "bengaluru west city corporation": ["Commissioner, Bengaluru West City Corporation (BWCC)", "commissioner.bwcc@gmail.com"],
  };

  // Karnataka jurisdiction lookup.
  //
  // Which body owns a road is a question the state already answers: KGIS holds the
  // boundary of every urban local body and returns the one containing a point, along
  // with its class and its national LGD code. Keying the officer directory on that code
  // rather than on a place name from a geocoder is what makes this work statewide: name
  // matching guessed, a point-in-polygon lookup does not.
  //
  // The rule that has not changed: a body we hold no verified address for is not routed.
  // Refusing is correct; addressing a citizen's complaint to a guess is not.
  const KGIS_TOWN_URL = "https://kgis.ksrsac.in/kgismaps/rest/services/Boundaries/Admin_Dynamic_New/MapServer/1/query";
  // State basemap road-ownership layers from the same KSRSAC service the boundaries
  // come from.  All three must answer before a municipal officer can be named.
  const KGIS_NH_URL = "https://kgis.ksrsac.in/kgismaps/rest/services/State_Basemap/State_Basemap_Dynamic/MapServer/289/query";
  const KGIS_SH_URL = "https://kgis.ksrsac.in/kgismaps/rest/services/State_Basemap/State_Basemap_Dynamic/MapServer/290/query";
  const KGIS_DH_URL = "https://kgis.ksrsac.in/kgismaps/rest/services/State_Basemap/State_Basemap_Dynamic/MapServer/291/query";
  const KGIS_GP_URL = "https://kgis.ksrsac.in/kgismaps/rest/services/Boundaries/GP_Boundary/MapServer/0/query";
  const OFFICER_TITLES = { CC: "Commissioner", CMC: "Chief Officer", TMC: "Chief Officer",
                           TP: "Chief Officer", NAC: "Chief Officer" };

  let _bodies = null;
  // A failure is never cached. Caching one meant a single slow read of a file that ships
  // inside the APK disabled routing for the rest of the session, and the app then refused
  // every report as having no address for its body. Local reads were measured at over four
  // seconds on a cold start, so this is not a remote possibility.
  async function bodies() {
    if (_bodies) return _bodies;
    try {
      const res = await fetchWithTimeout("karnataka-bodies.json", {}, 15000);
      const loaded = (await readJson(res)).bodies;
      if (loaded && Object.keys(loaded).length) { _bodies = loaded; return _bodies; }
    } catch (e) { /* fall through and retry on the next call */ }
    return {};
  }

  // Bengaluru is still resolvable without the network: the five corporations are the
  // common case and a demo should not depend on a state GIS being reachable.
  const BLR = { minLat: 12.70, maxLat: 13.25, minLng: 77.25, maxLng: 77.90 };
  function inCoverage(lat, lng, address) {
    if (lat != null && lng != null && !Number.isNaN(lat) && !Number.isNaN(lng)) {
      return lat >= BLR.minLat && lat <= BLR.maxLat && lng >= BLR.minLng && lng <= BLR.maxLng;
    }
    if (address) {
      const low = address.toLowerCase();
      return low.includes("bengaluru") || low.includes("bangalore");
    }
    return false; // no location at all: we cannot claim to know who is responsible
  }

  const DETECT_PROMPT = DETECTION_PROMPT_CONFIG.base;

  // Key order is the streaming order. The decision fields arrive before the factual
  // description, so the UI can update without using a made-up confidence percentage.
  const ASSESS_SCHEMA = DETECTION_PROMPT_CONFIG.schema;
  const TENDER_SCHEMA = TENDER_PROMPT_CONFIG.schema;
  const TENDER_MATCH_INSTRUCTIONS = TENDER_PROMPT_CONFIG.instructions;

  // ---------- OpenAI ----------
  const OAI_URL = RUNTIME_CONFIG.responsesUrl;
  const authHeaders = () => ({ "Content-Type": "application/json", "Authorization": `Bearer ${S.key}` });

  // Detection is a classification job, not an essay: left at its default the model
  // spends 200+ hidden reasoning tokens per photo before answering, which measured
  // as roughly 3.5 of the 6.5 seconds a verdict used to take.
  const withSpeedDefaults = (body) => ({
    ...body,
    // Detection inputs can contain precise road imagery and addresses. Do not retain
    // response application state beyond the request; provider abuse-monitoring rules
    // remain governed by OpenAI's published policy and are disclosed in our policy.
    store: RUNTIME_CONFIG.storeResponses,
    reasoning: (body && body.reasoning)
      || { effort: MODEL_CONFIG.reasoningEffortByModel[body && body.model]
        || MODEL_CONFIG.defaultReasoningEffort },
  });

  // Fatal means "fails the same way without streaming", so retrying plain is pointless.
  const fatal = (e) => { e.fatal = true; return e; };
  // A stalled request is worse than a failed one: without this a lost connection
  // leaves the UI on a spinner with no end, and a drive quietly stops forever.
  const REQUEST_TIMEOUT_MS = RUNTIME_CONFIG.timeoutsMs.personalOpenAI;
  // Fallback mode can spend up to 55s on OpenAI and then 30s on the project YOLO
  // gateway. Shared calls allow 15s more for Worker and network overhead.
  const SHARED_VISION_TIMEOUT_MS = RUNTIME_CONFIG.timeoutsMs.sharedVisionClient;

  // The timeout used to be cleared the moment the headers arrived, so it only ever covered
  // the handshake. A response that sent headers and then stalled was never aborted, and a
  // drive wedged with every slot occupied while the HUD went on reporting it healthy.
  //
  // The timer now stays armed until the body is finished. A streaming caller re-arms it on
  // every chunk that carries data, so a slow but live response is fine and a silent one is
  // not, and disarms it when the body is done.
  async function fetchWithTimeout(url, init, ms = REQUEST_TIMEOUT_MS) {
    const ctl = typeof AbortController !== "undefined" ? new AbortController() : null;
    let timer = setTimeout(() => ctl && ctl.abort(), ms);
    const disarm = () => { clearTimeout(timer); timer = null; };
    const rearm = (delay) => {
      if (timer === null) return;             // already finished
      clearTimeout(timer);
      timer = setTimeout(() => ctl && ctl.abort(), delay === undefined ? ms : delay);
    };
    let res;
    try {
      res = await fetch(url, ctl ? { ...init, signal: ctl.signal } : init);
    } catch (e) {
      disarm();
      if (e && (e.name === "AbortError" || /abort/i.test(e.message || ""))) {
        const to = new Error("The network did not respond. Check the connection and try again.");
        to.timeout = true;
        throw to;
      }
      // Platform network errors often expose transport internals. Keep the provider in
      // the message accurate now that this helper also serves the project service.
      const destination = String(url).startsWith(SERVICE_URL) ? "the reporting service" : "OpenAI";
      throw new Error(`Could not reach ${destination}. Check the connection and try again.`);
    }
    res.__disarm = disarm;
    res.__rearm = rearm;
    return res;
  }

  // Reading a body must always disarm the watchdog, including when it throws.
  async function readJson(res) {
    try { return await res.json(); }
    finally { if (res.__disarm) res.__disarm(); }
  }

  // ---------- central service identity and transport ----------
  // The install key is pseudonymous and non-extractable. It is used only to sign writes
  // to the project service; a personal OpenAI key stays on this device and continues to
  // go directly to OpenAI.
  const bytesToBase64 = (bytes) => {
    let binary = "";
    for (let i = 0; i < bytes.length; i++) binary += String.fromCharCode(bytes[i]);
    return btoa(binary);
  };
  const randomId = () => (globalThis.crypto && crypto.randomUUID ? crypto.randomUUID()
    : `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}-${Math.random().toString(36).slice(2)}`);
  const sha256HexBytes = async (bytes) => [...new Uint8Array(
    await crypto.subtle.digest("SHA-256", bytes))]
    .map((b) => b.toString(16).padStart(2, "0")).join("");
  const sha256HexText = async (text) => sha256HexBytes(new TextEncoder().encode(text));
  async function canonicalServiceRequest(method, urlOrPath, timestamp, idempotencyKey, exactBody) {
    const pathname = new URL(String(urlOrPath), `${SERVICE_URL}/`).pathname;
    const body = typeof exactBody === "string" ? exactBody : "";
    return `${String(method || "GET").toUpperCase()}\n${pathname}\n${String(timestamp || "")}\n`
      + `${String(idempotencyKey || "")}\n${await sha256HexText(body)}`;
  }

  let installationCache = null, installationPromise = null;
  async function loadInstallationIdentity() {
    if (installationCache) return installationCache;
    const nativeSigner = NATIVE && window.Capacitor && Capacitor.Plugins
      && Capacitor.Plugins.DriveMode
      && typeof Capacitor.Plugins.DriveMode.getCentralIdentity === "function"
      ? Capacitor.Plugins.DriveMode : null;
    if (nativeSigner) {
      const nativeIdentity = await nativeSigner.getCentralIdentity({ serviceUrl: SERVICE_URL });
      if (!nativeIdentity || !nativeIdentity.installId) {
        throw new Error("The reporting service could not register this installation.");
      }
      installationCache = {
        installId: String(nativeIdentity.installId), nativeSigner: true,
      };
      return installationCache;
    }
    if (typeof crypto === "undefined" || !crypto.subtle) {
      throw new Error("Secure device identity is unavailable on this phone.");
    }
    const cached = await op("readonly", (store) => store.get(INSTALLATION_KEY), "identity")
      .catch(() => null);
    if (cached && cached.installId && cached.privateKey) {
      installationCache = cached;
      return cached;
    }
    const pair = await crypto.subtle.generateKey(
      { name: "ECDSA", namedCurve: "P-256" }, false, ["sign", "verify"]);
    const publicRaw = new Uint8Array(await crypto.subtle.exportKey("raw", pair.publicKey));
    const body = JSON.stringify({ public_key: bytesToBase64(publicRaw) });
    const response = await fetchWithTimeout(`${SERVICE_URL}/v1/installations`, {
      method: "POST", headers: { "content-type": "application/json" }, body,
    }, 15000);
    const payload = await readJson(response).catch(() => ({}));
    if (!response.ok || !payload.install_id) {
      throw new Error(payload.message || "The reporting service could not register this installation.");
    }
    installationCache = {
      key: INSTALLATION_KEY, installId: String(payload.install_id),
      privateKey: pair.privateKey, publicKey: bytesToBase64(publicRaw),
    };
    await op("readwrite", (store) => store.put(installationCache), "identity");
    return installationCache;
  }

  function installationIdentity() {
    if (installationCache) return Promise.resolve(installationCache);
    if (!installationPromise) {
      installationPromise = loadInstallationIdentity().finally(() => { installationPromise = null; });
    }
    return installationPromise;
  }

  function serviceError(response, payload, fallback) {
    const message = payload && payload.message ? String(payload.message) : fallback;
    const err = new Error(message || "The reporting service could not complete the request.");
    err.code = payload && payload.error ? String(payload.error) : `service_${response.status}`;
    err.status = response.status;
    err.requestId = (payload && payload.request_id) || response.headers.get("x-request-id") || null;
    err.details = payload && payload.details && typeof payload.details === "object"
      ? payload.details : null;
    err.sharedService = true;
    if (/credit|budget|quota/i.test(err.code) || response.status === 402
        || response.status === 429 || response.status === 503) {
      err.fatal = true;
    }
    return err;
  }

  async function signedServicePost(path, value, options = {}) {
    let identity;
    try { identity = await installationIdentity(); }
    catch (error) { markProjectServiceUnavailable(); throw error; }
    // Durable retries sign and resend the original byte string. Re-stringifying a saved
    // object would normally be equivalent, but signing exact bytes makes that guarantee
    // explicit and keeps the server's idempotency record tied to one immutable request.
    const body = typeof options.exactBody === "string"
      ? options.exactBody : JSON.stringify(value == null ? {} : value);
    const timestamp = String(Date.now());
    const idempotencyKey = String(options.idempotencyKey || randomId());
    const pathname = new URL(`${SERVICE_URL}${path}`).pathname;
    const canonical = await canonicalServiceRequest("POST", pathname, timestamp, idempotencyKey, body);
    let installId = identity.installId;
    let signedTimestamp = timestamp;
    let signedIdempotencyKey = idempotencyKey;
    let signatureBase64;
    if (identity.nativeSigner) {
      const signer = Capacitor.Plugins.DriveMode;
      const signed = await signer.signCentralRequest({
        serviceUrl: SERVICE_URL, method: "POST", path: pathname,
        timestamp, idempotencyKey, body,
      });
      if (!signed || !signed.signature || !signed.installId) {
        throw new Error("The phone could not sign this reporting request.");
      }
      installId = String(signed.installId);
      signedTimestamp = String(signed.timestamp || timestamp);
      signedIdempotencyKey = String(signed.idempotencyKey || idempotencyKey);
      signatureBase64 = String(signed.signature);
    } else {
      const signature = new Uint8Array(await crypto.subtle.sign(
        { name: "ECDSA", hash: "SHA-256" }, identity.privateKey,
        new TextEncoder().encode(canonical)));
      signatureBase64 = bytesToBase64(signature);
    }
    let response;
    try {
      response = await fetchWithTimeout(`${SERVICE_URL}${path}`, {
        method: "POST",
        headers: {
          "content-type": "application/json",
          "X-Install-ID": installId,
          "X-Timestamp": signedTimestamp,
          "X-Signature": signatureBase64,
          "Idempotency-Key": signedIdempotencyKey,
        },
        body,
      }, options.timeout || REQUEST_TIMEOUT_MS);
    } catch (error) {
      markProjectServiceUnavailable();
      throw error;
    }
    const payload = await readJson(response).catch(() => ({}));
    if (!response.ok) {
      const error = serviceError(response, payload, options.fallback);
      if (response.status === 408 || response.status === 425 || response.status === 429
          || response.status >= 500) markProjectServiceUnavailable();
      throw error;
    }
    markProjectServiceAvailable();
    return payload;
  }

  async function serviceGet(path, timeout = 20000) {
    const response = await fetchWithTimeout(`${SERVICE_URL}${path}`, {
      headers: { "accept": "application/json" },
    }, timeout);
    const payload = await readJson(response).catch(() => ({}));
    if (!response.ok) throw serviceError(response, payload, "The reporting service is unavailable.");
    return payload;
  }

  // Personal mode can use the project service when it exists, but may never require it
  // to show a detector result. Shared mode uses the same bounded probe as its preflight,
  // so an outage becomes a clear error instead of an unbounded camera/analysis spinner.
  let projectServiceState = "unknown", projectServiceProbe = null, projectServiceCheckedAt = 0;
  const PROJECT_SERVICE_POSITIVE_TTL_MS = 30000;
  const projectServiceAvailable = () => projectServiceState === "available";
  function markProjectServiceAvailable() {
    projectServiceState = "available";
    projectServiceCheckedAt = Date.now();
  }
  function markProjectServiceUnavailable() {
    projectServiceState = "unavailable";
    projectServiceCheckedAt = Date.now();
  }
  function probeProjectService(timeout = 1500) {
    if (projectServiceAvailable()
        && Date.now() - projectServiceCheckedAt < PROJECT_SERVICE_POSITIVE_TTL_MS) {
      return Promise.resolve(true);
    }
    if (projectServiceAvailable()) projectServiceState = "unknown";
    if (projectServiceState === "unavailable" && Date.now() - projectServiceCheckedAt < 60000) {
      return Promise.resolve(false);
    }
    if (projectServiceProbe) return projectServiceProbe;
    projectServiceProbe = serviceGet("/v1/health", timeout)
      .then((result) => {
        if (result && result.ok) markProjectServiceAvailable();
        else markProjectServiceUnavailable();
        return projectServiceAvailable();
      })
      .catch(() => {
        markProjectServiceUnavailable();
        return false;
      })
      .finally(() => { projectServiceProbe = null; });
    return projectServiceProbe;
  }

  // Never surface a provider's response body: it is JSON, it is long, and on a
  // projected screen it reads as a crash.
  async function statusError(res) {
    const bad = mapStatus(res);
    if (bad) return bad;
    if (res.status === 400) return new Error("OpenAI rejected the request. If this persists, the app needs an update.");
    if (res.status === 408 || res.status === 504) return new Error("OpenAI timed out. Try again.");
    if (res.status >= 500) return new Error("OpenAI is having trouble right now. Try again in a moment.");
    return new Error("OpenAI could not process that image. Try again.");
  }

  function mapStatus(res) {
    if (res.status === 401) return fatal(new Error("OpenAI rejected the API key. Check it in settings."));
    if (res.status === 403) return fatal(new Error("This API key is not allowed to use the model. Check the key in settings."));
    // 429 covers both throttling and an exhausted balance, and telling someone to
    // wait a minute for a spent quota sends them in circles.
    if (res.status === 429) return fatal(new Error("OpenAI refused: rate limit or the key's credit is exhausted. Check the account."));
    return null;
  }

  async function oai(body) {
    if (!S.key) throw new Error("OpenAI API key missing. Tap the gear icon and paste it.");
    const res = await fetchWithTimeout(OAI_URL, {
      method: "POST", headers: authHeaders(), body: JSON.stringify(withSpeedDefaults(body)),
    });
    if (!res.ok) throw await statusError(res);
    const data = await readJson(res);
    const msg = (data.output || []).find((o) => o.type === "message");
    const text = msg && msg.content && msg.content.find((c) => c.type === "output_text");
    if (!text || !text.text) throw new Error("Empty model response.");
    return JSON.parse(text.text);
  }

  // Structured outputs stream in schema order. Only closed string values are read:
  // a partial `"pothole_cav` must never become a decision. The same semantic helper is
  // used for the streamed and final paths so the UI cannot announce a result that the
  // pipeline later reverses.
  const QUALITY_RE = /"image_quality"\s*:\s*"(acceptable|rejected)"/;
  const ASSESSMENT_RE = /"assessment"\s*:\s*"(damaged|undamaged)"/;
  const DAMAGE_RE = /"damage_type"\s*:\s*(null|"(?:pothole_cavity|failed_patch|surface_breakup|rut_or_depression|other_road_damage)")/;
  const SIZE_RE = /"size"\s*:\s*(null|"(?:small|medium|large)")/;
  const schemaStrings = (field) => new Set(
    ASSESS_SCHEMA.properties[field].enum.filter((value) => typeof value === "string"));
  const DAMAGE_TYPES = schemaStrings("damage_type");
  const SIZES = schemaStrings("size");

  function decisionFor(a) {
    if (!a || a.image_quality !== "acceptable") return "review";
    const hasDamageType = DAMAGE_TYPES.has(a.damage_type);
    const validSize = a.size === null || SIZES.has(a.size);
    if (a.assessment === "damaged") {
      return hasDamageType && validSize ? "accept" : "review";
    }
    if (a.assessment === "undamaged") {
      // Non-null damage details contradict an undamaged result.
      if (a.damage_type !== null || a.size !== null) return "review";
      return "reject";
    }
    return "review";
  }

  function partialAssessment(text) {
    const q = QUALITY_RE.exec(text), a = ASSESSMENT_RE.exec(text);
    const d = DAMAGE_RE.exec(text), s = SIZE_RE.exec(text);
    if (!a || !q || !d || !s) return null;
    return {
      image_quality: q[1], assessment: a[1],
      damage_type: d[1] === "null" ? null : d[1].slice(1, -1),
      size: s[1] === "null" ? null : s[1].slice(1, -1),
    };
  }

  const peekVerdict = (partial) => {
    const a = partialAssessment(partial);
    if (!a) return null;
    const decision = decisionFor(a);
    return { accepted: decision === "accept", review: decision === "review",
             damage_type: a.damage_type, assessment: a.assessment };
  };

  // True once the response has proved that Drive Mode will not create a complaint.
  // Debug/evaluation calls do not enable cancellation because they need the exact full
  // verdict, including the reason for a miss.
  const peekReject = (partial) => {
    const quality = QUALITY_RE.exec(partial);
    if (quality && quality[1] === "rejected") return true;
    const a = peekVerdict(partial);
    return !!a && !a.accepted && !a.review;
  };

  function drainSSE(chunk, state, onEarly, stopWhenRejected) {
    state.buf += chunk;
    let i;
    while ((i = state.buf.indexOf("\n")) >= 0) {
      const line = state.buf.slice(0, i).trim();
      state.buf = state.buf.slice(i + 1);
      if (!line.startsWith("data:")) continue;
      const payload = line.slice(5).trim();
      if (!payload || payload === "[DONE]") continue;
      let ev;
      try { ev = JSON.parse(payload); } catch (e) { continue; }
      if (ev.type === "response.output_text.delta" && typeof ev.delta === "string") {
        state.text += ev.delta;
        if (!state.early && onEarly) {
          const v = peekVerdict(state.text);
          if (v) { state.early = true; try { onEarly(v); } catch (e) {} }
        }
        if (stopWhenRejected && !state.stop && peekReject(state.text)) state.stop = true;
      }
    }
  }

  async function oaiStream(body, onEarly, stopWhenRejected) {
    if (!S.key) throw new Error("OpenAI API key missing. Tap the gear icon and paste it.");
    const res = await fetchWithTimeout(OAI_URL, {
      method: "POST", headers: authHeaders(),
      body: JSON.stringify(withSpeedDefaults({
        ...body, stream: RUNTIME_CONFIG.personalDetectionStream,
      })),
    });
    if (!res.ok) throw await statusError(res);

    const state = { buf: "", text: "", early: false, stop: false };
    if (res.body && typeof res.body.getReader === "function") {
      const reader = res.body.getReader();
      const dec = new TextDecoder();
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        // A chunk that carries data proves the response is alive, so the watchdog resets.
        // A response that goes silent mid-body is aborted rather than hanging the drive.
        if (res.__rearm) res.__rearm();
        drainSSE(dec.decode(value, { stream: true }), state, onEarly, stopWhenRejected);
        if (state.stop) { try { await reader.cancel(); } catch (e) {} break; }
      }
    } else {
      // Buffered transports (the native HTTP bridge) hand back the whole SSE body at once,
      // so there is nothing left to stop early: the tokens were already generated.
      try { drainSSE(await res.text(), state, onEarly, stopWhenRejected); }
      finally { if (res.__disarm) res.__disarm(); }
    }
    if (res.__disarm) res.__disarm();
    if (state.stop) return rejectedVerdict(state.text);
    drainSSE("\n", state, onEarly, stopWhenRejected);
    if (state.stop) return rejectedVerdict(state.text);
    if (!state.text) throw new Error("Empty model response.");
    return JSON.parse(state.text);
  }

  // Reconstructed after the streamed fields prove an acceptable, undamaged image. The
  // remaining nullable fields have only one schema-consistent value for that verdict.
  function rejectedVerdict(text) {
    const a = ASSESSMENT_RE.exec(text), q = QUALITY_RE.exec(text);
    const d = DAMAGE_RE.exec(text), s = SIZE_RE.exec(text);
    return {
      image_quality: q ? q[1] : "acceptable",
      assessment: a ? a[1] : "undamaged",
      damage_type: d && d[1] !== "null" ? d[1].slice(1, -1) : null,
      size: s && s[1] !== "null" ? s[1].slice(1, -1) : null,
      description: "",
    };
  }

  const fmt = (name, schema) => ({
    format: { type: "json_schema", name, schema,
      strict: RUNTIME_CONFIG.strictStructuredOutputs },
    verbosity: RUNTIME_CONFIG.textVerbosity,
  });
  const progress = (m) => { try { window.dispatchEvent(new CustomEvent("pipeline-progress", { detail: m })); } catch (e) {} };
  const emitVerdict = (v) => { try { window.dispatchEvent(new CustomEvent("pipeline-verdict", { detail: v })); } catch (e) {} };

  function buildDetectionRequest(imageInputs, prompt, model = S.model, detail = S.detail) {
    const selectedModel = normaliseModel(model);
    const selectedDetail = normaliseDetail(detail, selectedModel);
    const supplied = (Array.isArray(imageInputs) ? imageInputs : [imageInputs])
      .find((x) => x && (typeof x === "string" ? x : x.url));
    if (!supplied) throw new Error("No usable image supplied for detection.");
    const image = typeof supplied === "string" ? { url: supplied } : supplied;
    const content = [
      { type: "input_image", image_url: image.url,
        detail: normaliseDetail(image.detail || selectedDetail, selectedModel) },
      { type: "input_text", text: prompt },
    ];
    return {
      model: selectedModel,
      input: [{ role: DETECTION_PROMPT_CONFIG.role, content }],
      text: fmt(DETECTION_PROMPT_CONFIG.schemaName, ASSESS_SCHEMA),
    };
  }

  async function analyzeViaService(imageInputs, model, detail, captureMode, idempotencyKey,
                                   observation) {
    const selectedModel = normaliseModel(model);
    const selectedDetail = normaliseDetail(detail, selectedModel);
    const supplied = (Array.isArray(imageInputs) ? imageInputs : [imageInputs])
      .find((item) => item && (typeof item === "string" ? item : item.url));
    if (!supplied) throw new Error("No usable image supplied for detection.");
    const images = [{
      data_url: typeof supplied === "string" ? supplied : supplied.url,
    }];
    const body = {
      images,
      capture_mode: captureMode === "drive" ? "drive" : "manual",
      language: LANG(),
      model: selectedModel,
      image_detail: selectedDetail,
      prompt_version: PROMPT_VERSION,
    };
    // The server-issued receipt binds an accepted result to this stable observation and
    // location. The later map write repeats the same values, preventing a caller from
    // turning one paid detection into arbitrary impact-map points.
    if (observation && observation.client_observation_id) {
      body.client_observation_id = String(observation.client_observation_id);
    }
    if (observation && finiteCoord(observation.lat) && finiteCoord(observation.lng)) {
      body.lat = observation.lat;
      body.lng = observation.lng;
    }
    const payload = await signedServicePost("/v1/vision/detect", body, {
      idempotencyKey: idempotencyKey || randomId(),
      fallback: "The shared vision service could not check that image.",
      timeout: SHARED_VISION_TIMEOUT_MS,
    });
    // Never manufacture a negative verdict from an error or malformed success. A full
    // Complete schema output is required before the normal local decision gate can run.
    for (const field of ASSESS_SCHEMA.required) {
      if (!Object.prototype.hasOwnProperty.call(payload, field)) {
        const err = new Error("The shared vision service returned an incomplete result. Try again.");
        err.sharedService = true;
        throw err;
      }
    }
    return payload;
  }

  let streamBroken = false;
  async function analyzeImage(imageInputs, prompt, name, schema, model, onEarly, stopWhenRejected, detail,
                              captureMode = "manual", idempotencyKey = null,
                              observation = null) {
    if (schema === ASSESS_SCHEMA) {
      if (usingSharedVision()) {
        return analyzeViaService(
          imageInputs, model, detail, captureMode, idempotencyKey, observation);
      }
      // Personal-key images and verdicts stay between this device and OpenAI. This small,
      // signed counter lets the project measure usage even when no pothole is accepted.
      // It intentionally runs in parallel and cannot change or delay the vision verdict.
      void probeProjectService().then((available) => {
        if (!available) return;
        return signedServicePost("/v1/activity", {
          event: "vision_check",
          vision_provider: "personal_openai",
          capture_mode: captureMode === "drive" ? "drive" : "manual",
        }, {
          idempotencyKey: randomId(), timeout: 15000,
          fallback: "The anonymous activity count could not be recorded.",
        });
      }).catch(() => {});
    }
    if (schema !== ASSESS_SCHEMA) throw new Error("Unsupported vision request.");
    const body = buildDetectionRequest(imageInputs, prompt, model, detail);
    if ((!onEarly && !stopWhenRejected) || streamBroken) return oai(body);
    try {
      return await oaiStream(body, onEarly, stopWhenRejected);
    } catch (e) {
      // A bad key or a rate limit fails identically unstreamed, so surface those.
      if (e && e.fatal) throw e;
      // A timeout says nothing about whether the server can stream, it says the network
      // stalled. Retrying it unstreamed stalls again, so a single stalled frame cost two
      // full timeouts, and latching streamBroken made every later frame pay for streaming
      // it would no longer use. Surface it and leave streaming alone.
      if (e && (e.timeout || e.name === "AbortError")) {
        // The abort can surface from the body reader rather than from fetch, where it
        // arrives as a bare "Aborted". Nobody watching a demo should be shown that.
        if (e.timeout) throw e;
        const to = new Error("The network did not respond. Check the connection and try again.");
        to.timeout = true;
        throw to;
      }
      // Anything else (a server that refuses stream:true, a transport that cannot
      // stream, a parse failure) must not cost us the verdict. Remember it, so the
      // wasted round trip is paid once per launch and not on every photo.
      streamBroken = true;
      return oai(body);
    }
  }

  // One warm TLS connection ahead of the first real call. Costs no tokens.
  let warmedAt = 0;
  async function prewarm() {
    if (usingSharedVision() || !S.key || Date.now() - warmedAt < 60000) return;
    warmedAt = Date.now();
    try {
      await fetch(RUNTIME_CONFIG.modelsUrl, { headers: authHeaders() });
    } catch (e) {}
  }

  // ---------- location ----------
  // The officer knows which state and country they work in. A complaint that says
  // "..., Bengaluru Central City Corporation, Bengaluru, Bangalore North, Bengaluru
  // Urban, Karnataka, 560052, India" reads like machine output, so the address is built
  // from the parts that actually locate the pothole: street, locality, city, pincode.
  // display_name is kept only for the offline routing fallback, which needs the
  // corporation name that this trimming deliberately drops.
  async function reverseGeocode(lat, lng) {
    try {
      const res = await fetchWithTimeout(
        `https://nominatim.openstreetmap.org/reverse?lat=${lat}&lon=${lng}&format=jsonv2&zoom=17&addressdetails=1`,
        {}, 12000);
      if (!res.ok) return null;
      const d = await readJson(res);
      const a = d.address || {};
      const parts = [
        a.road || a.pedestrian || a.residential || a.footway,
        a.neighbourhood || a.hamlet,
        a.suburb || a.village,
        a.city || a.town || a.municipality,
        a.postcode,
      ].filter((x, i, all) => x && all.indexOf(x) === i);
      return { short: parts.join(", ") || d.display_name || null, full: d.display_name || null };
    } catch (e) { return null; }
  }

  // Asks the state which body contains this point. Returns null when the point is
  // outside every urban local body, which is the rural case: those roads belong to PWD
  // or the panchayat engineering department, not to a municipality.
  const kgisPoint = (base, lat, lng, fields) => {
    const geometry = encodeURIComponent(JSON.stringify(
      { x: lng, y: lat, spatialReference: { wkid: 4326 } }));
    return `${base}?geometry=${geometry}&geometryType=esriGeometryPoint`
      + `&spatialRel=esriSpatialRelIntersects&outFields=${fields}&returnGeometry=false&f=json`;
  };

  // Three outcomes, and telling them apart is the whole point. A town means a municipal
  // body owns the road. No town but a gram panchayat means rural Karnataka, which belongs
  // to PWD or the panchayat engineering department. Neither means the point is outside
  // Karnataka altogether. An empty features array is a normal 200, not an error.
  // One shared retry for the state GIS. Both callers fail closed on null, so a blip
  // costs a refusal the user can retry, never a wrong answer stated confidently.
  async function retryQuery(url, lat, lng, fields) {
    // Measured against the live service: it answers in 383 to 692 ms most of the time and
    // occasionally takes over seven seconds, and one request in ten timed out at eight.
    // Since this check fails closed, a slow government server was turning into a refusal
    // for roughly one report in ten. Three attempts at twelve seconds costs nothing on the
    // common path and makes that rare.
    for (let attempt = 0; attempt < 3; attempt++) {
      try {
        const r = await fetchWithTimeout(kgisPoint(url, lat, lng, fields), {}, 12000);
        if (r.ok) return r;
      } catch (e) { /* fall through to the retry */ }
      if (attempt < 2) await new Promise((res) => setTimeout(res, 300 * (attempt + 1)));
    }
    return null;
  }

  // ArcGIS reports failures as HTTP 200 with an error body and no features array, so a
  // bad query, an unavailable layer or a service error all look identical to "nothing
  // here" if the array is defaulted to empty. On the highway layer that meant the gate
  // failed OPEN and a national highway was addressed to a municipal officer. Returns null
  // when the service did not actually answer.
  const featuresOf = (body) =>
    (body && Array.isArray(body.features) && !body.error) ? body.features : null;

  async function kgisJurisdiction(lat, lng) {
    // Which polygon contains this point answers WHERE the pothole is, not WHO owns the
    // road. A national highway is a line that crosses town boundaries, so containment
    // alone addressed a Commissioner for NHAI's carriageway: measured, 5 of 12 verified
    // NH points in Karnataka routed to a municipal officer. The state's own basemap has
    // the highway network, on the host this already calls, so the two questions are asked
    // together and the answer costs no extra wait.
    const [town, nh, sh, dh] = await Promise.all([
      retryQuery(KGIS_TOWN_URL, lat, lng, "KGISTownName,Town_Type,KGISTownCode,LGD_TownCode"),
      // Exact containment only. A buffer picks up OBJECTID 3059, Bengaluru's MG Road,
      // which this land-cover layer misclassifies as National Highway, and that would
      // start refusing genuine city reports in the densest coverage area.
      //
      // Retried once, because this check fails closed: a momentary blip on the state's
      // server refuses a report the app could have routed, and there are now two calls
      // per report where there used to be one.
      retryQuery(KGIS_NH_URL, lat, lng, "Name"),
      retryQuery(KGIS_SH_URL, lat, lng, "Name"),
      retryQuery(KGIS_DH_URL, lat, lng, "Name"),
    ]);
    if (!town) return { kind: "road_class_unknown" };
    // Fail closed, but not into offline(): that fallback only knows Bengaluru, so a
    // failed highway check there refused every report in the rest of the state and
    // called it "outside Karnataka". An unanswered road-class check is its own outcome.
    if ([nh, sh, dh].some((response) => !response || !response.ok)) {
      return { kind: "road_class_unknown" };
    }
    const highwayLayers = await Promise.all([
      readJson(nh).then(featuresOf),
      readJson(sh).then(featuresOf),
      readJson(dh).then(featuresOf),
    ]);
    // A missing features array means the service did not answer the question. Reading it
    // as "no highway here" is the same failure as not asking at all.
    if (highwayLayers.some((features) => features === null)) {
      return { kind: "road_class_unknown" };
    }
    const ownershipKinds = ["national_highway", "state_highway", "district_highway"];
    for (let index = 0; index < highwayLayers.length; index++) {
      if (!highwayLayers[index].length) continue;
      const road = ((highwayLayers[index][0].attributes || {}).Name || "").trim();
      return { kind: ownershipKinds[index], name: road || null };
    }
    const t = featuresOf(await readJson(town));
    if (t === null) return { kind: "road_class_unknown" };
    if (t.length) {
      const a = t[0].attributes || {};
      return { kind: "town", name: a.KGISTownName || null,
               type: (a.Town_Type || "").trim().toUpperCase(),
               lgd: a.LGD_TownCode ? String(a.LGD_TownCode) : "" };
    }
    // Electronics City is the one town row with a blank type and no codes, so it lands
    // here as a named body with no LGD: still refused, but by name rather than silently.
    // "Outside Karnataka" is only true when the state actually answered and placed the
    // point in no town and no panchayat. If this query fails, we know nothing, and
    // saying "outside Karnataka" to someone standing on a village road in Magadi is
    // simply a lie. Retried for the same reason the highway check is.
    const gp = await retryQuery(KGIS_GP_URL, lat, lng, "KGISGPName");
    if (!gp) return { kind: "road_class_unknown" };
    const g = featuresOf(await readJson(gp));
    if (g === null) return { kind: "road_class_unknown" };
    const name = g.length && (g[0].attributes || {}).KGISGPName;
    if (name && String(name).trim()) return { kind: "rural", name: String(name).trim() };
    return { kind: "outside_state" };
  }

  // Resolves the officer to address, or [null, null] with a reason. Every path that
  // cannot name a real body returns nothing rather than a plausible-looking guess.
  // One GIS answer per location, shared by contract lookup and officer routing. Both
  // need it, and asking twice would double the latency of the one network call on the
  // critical path.
  let _jurKey = null, _jurP = null;
  function jurisdictionOf(lat, lng) {
    const key = `${lat},${lng}`;
    if (_jurKey !== key) { _jurKey = key; _jurP = kgisJurisdiction(lat, lng); }
    return _jurP;
  }

  function routeWhereFromCentral(jurisdiction) {
    if (!jurisdiction || typeof jurisdiction !== "object") return null;
    const ownership = String(jurisdiction.road_ownership || "");
    if (ownership === "municipal") {
      return { kind: "town", name: jurisdiction.town || null,
               type: jurisdiction.town_type || "", lgd: jurisdiction.lgd || "" };
    }
    if (["national_highway", "state_highway", "district_highway"].includes(ownership)) {
      return { kind: ownership, name: jurisdiction.highway_name || null };
    }
    if (ownership === "rural") {
      return { kind: "rural", name: jurisdiction.rural_body || null };
    }
    if (ownership === "outside_state") return { kind: "outside_state" };
    return { kind: "road_class_unknown" };
  }

  async function routeOfficer(address, lat, lng, authoritativeJurisdiction = null) {
    if (lat == null || lng == null || Number.isNaN(lat) || Number.isNaN(lng)) {
      return [null, null, "no_location"];
    }

    // If the state cannot tell us what this road is, we do not name anyone. The highway
    // check lives inside kgisJurisdiction, so any path that routes without it can address
    // a Commissioner for a carriageway NHAI owns: with the GIS blocked, NH48 at
    // Nelamangala produced a send-ready draft to comm@bbmp.gov.in, because it sits inside
    // the old Bengaluru bounding box that the fallback trusted.
    //
    // Nothing is lost by refusing. Detection itself needs the network, so there is no
    // offline report to route; this path only fires when OpenAI is reachable and the
    // state GIS is not, and in that case the road's owner is genuinely unknown.
    let where;
    try {
      where = authoritativeJurisdiction
        ? routeWhereFromCentral(authoritativeJurisdiction)
        : await jurisdictionOf(lat, lng);
    }
    catch (e) { return [null, null, "road_class_unknown"]; }

    if (where.kind === "outside_state") return [null, null, "outside_area"];
    if (where.kind === "national_highway") return [null, null, "national_highway", where.name];
    if (where.kind === "state_highway") return [null, null, "state_highway", where.name];
    if (where.kind === "district_highway") return [null, null, "district_highway", where.name];
    if (where.kind === "road_class_unknown") return [null, null, "road_class_unknown"];
    if (where.kind === "rural") return [null, null, "rural_road", where.name];

    const registry = await bodies();
    const entry = where.lgd && registry[where.lgd];
    if (!entry || !entry.email) return [null, null, "no_address_for_body", where.name];
    const title = entry.officer || OFFICER_TITLES[entry.type || where.type] || "Chief Officer";
    return [`${title}, ${entry.name}${entry.short ? ` (${entry.short})` : ""}`, entry.email, null];
  }

  function unroutedComplaintMessage(reason) {
    return {
      no_location: "This report has no location, so there is no way to tell which office is responsible. Retake it with location switched on.",
      road_class_unknown: "The app could not check whether this road is a national, state, or district highway, and it will not name a city officer for a road that may not be theirs. Try again when you have a signal.",
      national_highway: "This stretch is a national highway. It is maintained by NHAI or the state PWD National Highways division, not by the city or town body, so there is no municipal officer to address.",
      state_highway: "This stretch is a state highway. It is maintained by the state PWD, not by the city or town body, so there is no municipal officer to address.",
      district_highway: "This stretch is a district highway. It is maintained by the district or state road authority, not by the city or town body, so there is no municipal officer to address.",
      rural_road: "This road is outside every town boundary, so it belongs to the state PWD or a panchayat rather than a city body. The app will not guess an office.",
      no_address_for_body: "This town's body is known, but no official email address for it has been published, so there is no verified recipient to address.",
      outside_area: "This road damage is outside Karnataka, which is the area this app covers, so there is no authority to address.",
    }[reason] || "This report could not be routed to a responsible office, so there is no verified email recipient.";
  }

  function complaintRouteError(reason, body, details = {}) {
    const error = new Error(unroutedComplaintMessage(reason));
    error.code = "complaint_unrouted";
    error.unroutedReason = reason;
    error.unroutedBody = body || null;
    Object.assign(error, details);
    return error;
  }

  function distMeters(lat1, lng1, lat2, lng2) {
    const rad = Math.PI / 180;
    const dLat = (lat2 - lat1) * rad, dLng = (lng2 - lng1) * rad;
    const a = Math.sin(dLat / 2) ** 2 + Math.cos(lat1 * rad) * Math.cos(lat2 * rad) * Math.sin(dLng / 2) ** 2;
    return 2 * 6371000 * Math.asin(Math.sqrt(a));
  }

  const finiteCoord = (v) => typeof v === "number" && Number.isFinite(v);
  const acceptedReport = (r) => !!r
    && (r.decision === "accept" || ACCEPTED_REPORT_STATUSES.has(r.status));
  const storedDamageType = (r) => r && (r.damage_type || (r.is_pothole ? "pothole_cavity" : null));
  const localDamageFamily = new Set(["pothole_cavity", "failed_patch"]);
  const compatibleDamage = (a, b) => {
    const left = storedDamageType(a), right = storedDamageType(b);
    return !!left && (left === right || (localDamageFamily.has(left) && localDamageFamily.has(right)));
  };
  const sizeConflict = (a, b) => !!a.size && !!b.size
    && ((a.size === "small" && b.size === "large") || (a.size === "large" && b.size === "small"));
  const eventTime = (r) => Number.isFinite(r.last_seen_at) ? r.last_seen_at
    : Number.isFinite(r.captured_at) ? r.captured_at : r.created_at;
  const headingDifference = (a, b) => {
    const d = Math.abs(a - b) % 360;
    return Math.min(d, 360 - d);
  };
  const eventSighting = (r) => ({
    drive_id: r.drive_id == null ? null : String(r.drive_id),
    lat: finiteCoord(r.lat) ? r.lat : null,
    lng: finiteCoord(r.lng) ? r.lng : null,
    source_offset_s: Number.isFinite(r.source_offset_s) ? r.source_offset_s : null,
    captured_at: Number.isFinite(r.captured_at) ? r.captured_at : null,
    gps_accuracy: Number.isFinite(r.gps_accuracy) ? r.gps_accuracy : null,
    speed_mps: Number.isFinite(r.speed_mps) ? r.speed_mps : null,
    heading: Number.isFinite(r.heading) ? r.heading : null,
    source_event_key: r.source_event_key || null,
  });
  const storedSightings = (r) => Array.isArray(r.event_sightings) && r.event_sightings.length
    ? r.event_sightings.map((seen) => ({ ...seen,
        drive_id: seen.drive_id == null && r.drive_id != null ? String(r.drive_id) : seen.drive_id }))
    : [eventSighting(r)];

  function matchesEverySameDriveSighting(candidate, sightings) {
    // A normal 4-second cluster contains only a handful of samples. If corrupted or
    // imported data exceeds this bound, save a separate event instead of dropping one.
    if (sightings.length > 64) return false;
    return sightings.every((seen) => {
      const delta = (a, b) => Number.isFinite(a) && Number.isFinite(b) ? Math.abs(a - b) : Infinity;
      const seconds = Math.min(delta(candidate.source_offset_s, seen.source_offset_s),
                               delta(candidate.captured_at, seen.captured_at));
      if (!Number.isFinite(seconds)) return false;
      const positioned = finiteCoord(candidate.lat) && finiteCoord(candidate.lng)
        && finiteCoord(seen.lat) && finiteCoord(seen.lng);
      const accuracyPoor = !Number.isFinite(candidate.gps_accuracy) || !Number.isFinite(seen.gps_accuracy)
        || candidate.gps_accuracy > 30 || seen.gps_accuracy > 30;
      if (!positioned || accuracyPoor) return seconds <= DEDUPE_POOR_GPS_S;
      const distance = distMeters(candidate.lat, candidate.lng, seen.lat, seen.lng);
      const stationary = Number.isFinite(candidate.speed_mps) && Number.isFinite(seen.speed_mps)
        && candidate.speed_mps <= 1 && seen.speed_mps <= 1;
      return stationary
        ? seconds <= 30 && distance <= 5
        : seconds <= DEDUPE_SAME_DRIVE_S && distance <= DEDUPE_ADJACENT_RADIUS_M;
    });
  }

  // A GPS match works across drives and app restarts. The time match is deliberately
  // limited to one drive: it recovers recorded footage with no GPS, but cannot merge two
  // unrelated manual reports merely because they were processed at the same time.
  function roadEventMatch(candidate, prior) {
    if (!candidate.dedupe_eligible || !acceptedReport(prior)
        || prior.debug_capture || prior.dedupe_eligible === false) return null;
    // A fresh routable complaint is more useful than an old accepted observation that
    // could not name an authority. Never hide the sendable one behind the unrouted one.
    if (candidate.status === "draft" && prior.status === "unrouted") return null;
    const keys = Array.isArray(prior.source_event_keys) ? prior.source_event_keys : [];
    if (candidate.source_event_key
        && (prior.source_event_key === candidate.source_event_key || keys.includes(candidate.source_event_key))) {
      return { kind: "same_source" };
    }
    // A manual photo is an explicit user action. Do not silently swallow it based on
    // approximate phone GPS; automatic Drive/VOD observations are the duplicate source.
    if (candidate.capture_source === "manual") return null;
    if (!compatibleDamage(candidate, prior) || sizeConflict(candidate, prior)) return null;
    const positioned = finiteCoord(candidate.lat) && finiteCoord(candidate.lng)
      && finiteCoord(prior.lat) && finiteCoord(prior.lng);
    const distance = positioned
      ? distMeters(candidate.lat, candidate.lng, prior.lat, prior.lng) : Infinity;
    const candidateDrive = candidate.drive_id == null ? null : String(candidate.drive_id);
    const sameDriveSightings = candidateDrive == null ? []
      : storedSightings(prior).filter((seen) => seen.drive_id != null
          && String(seen.drive_id) === candidateDrive);
    if (sameDriveSightings.length) return matchesEverySameDriveSighting(candidate, sameDriveSightings)
      ? { kind: "same_drive" } : null;

    // A repeat on another drive is less certain: require recent, precise GPS, compatible
    // scale and subtype, and travel direction when the phone supplied it. Missing heading
    // is allowed only at a tighter radius so existing v1.12 reports still protect users.
    if (!positioned || !Number.isFinite(candidate.gps_accuracy) || !Number.isFinite(prior.gps_accuracy)
        || candidate.gps_accuracy > 15 || prior.gps_accuracy > 15) return null;
    const age = Math.abs((eventTime(candidate) || 0) - (eventTime(prior) || 0));
    if (!Number.isFinite(age) || age > DEDUPE_HISTORY_S) return null;
    const left = storedDamageType(candidate), right = storedDamageType(prior);
    if (left === "other_road_damage" || right === "other_road_damage") return null;
    let radius = left === right ? DEDUPE_HISTORY_RADIUS_M : 5;
    const moving = Number.isFinite(candidate.speed_mps) && Number.isFinite(prior.speed_mps)
      && candidate.speed_mps >= 2 && prior.speed_mps >= 2;
    const headingsKnown = Number.isFinite(candidate.heading) && Number.isFinite(prior.heading);
    if (moving && headingsKnown) {
      if (headingDifference(candidate.heading, prior.heading) > 45) return null;
    } else {
      radius = Math.min(radius, DEDUPE_MISSING_HEADING_RADIUS_M);
    }
    return distance <= radius ? { kind: "prior_drive" } : null;
  }

  const sameRoadEvent = (candidate, prior) => !!roadEventMatch(candidate, prior);

  function findDuplicateReport(candidate, reports) {
    for (let i = reports.length - 1; i >= 0; i--) {
      if (sameRoadEvent(candidate, reports[i])) return reports[i];
    }
    return null;
  }

  // ---------- tenders ----------
  let _tenders = null;
  // Parsed once per app session, which matters far more now the file is 9.5 MB: the
  // bundled data cannot change while the app runs, so one parse is correct.
  // As with the registry: a failed read is not cached, or one slow start would silently
  // stop every complaint naming a contract for the rest of the session.
  async function tenders() {
    if (_tenders) return _tenders;
    try {
      const res = await fetchWithTimeout("tenders.json", {}, 30000);
      const loaded = await readJson(res);
      if (Array.isArray(loaded) && loaded.length) { _tenders = loaded; return _tenders; }
    } catch (e) { /* fall through and retry on the next call */ }
    return [];
  }

  // Contracts grouped by the body that awarded them, built once. Each municipal row
  // carries the LGD code of its body (stamped by tools/index-tenders.py), and the state
  // GIS gives the same code for the pothole, so picking the right shortlist is a lookup
  // rather than a scan of every municipal contract in Karnataka.
  let _byBody = null;
  async function tendersFor(lgd) {
    if (!_byBody) {
      // Built from whatever tenders() returned, and only kept if that was a real load.
      // Caching an index built from a failed read repeats the same fault one level up.
      const index = new Map();
      for (const t of await tenders()) {
        if (!t.b) continue;
        let list = index.get(t.b);
        if (!list) { list = []; index.set(t.b, list); }
        list.push(t);
      }
      if (!index.size) return [];
      _byBody = index;
    }
    if (!lgd) return [];
    const own = _byBody.get(String(lgd)) || [];
    // Bengaluru's five corporations replaced BBMP in 2025 and inherited its works, which
    // the award records still file under BBMP zones. Zone to corporation is not
    // published, so all five share that legacy pool.
    const legacy = BLR_BODIES.has(String(lgd)) ? (_byBody.get("BLR") || []) : [];
    return legacy.length ? own.concat(legacy) : own;
  }


  // The five corporations that replaced BBMP in 2025 and share its legacy contract pool.
  const BLR_BODIES = new Set(["305850", "305851", "305852", "305853", "305854"]);

  const TENDER_STOP = new Set(["road", "roads", "street", "cross", "main", "layout", "bengaluru", "bangalore",
    "karnataka", "india", "ward", "city", "corporation", "south", "north", "east",
    "west", "central", "urban", "sector", "stage", "block", "phase"]);

  // A publication date says when a notice was published. It does not reveal the award,
  // completion, defect-liability or maintenance dates, so it must never be converted into
  // a current-liability claim about a contractor. Keep this helper for one canonical value
  // across central, local and previously stored tender records.
  function warrantyFor(_published, _now) {
    return {
      warranty: "current liability not established by the publication record",
      warranty_code: "unverified",
    };
  }

  // The ranked candidate list, split out from matchTender so it can be tested on its own.
  // It is entirely local and must be deterministic: the same address and body must give
  // the same list in the same order every time, or the app cannot justify the contract it
  // eventually prints in a letter naming a real company.
  async function shortlistFor(address, lgd) {
    if (!address || !lgd) return [];
    const tokens = new Set();
    for (const part of address.split(",").slice(0, 4)) {
      for (const w of part.trim().toLowerCase().replace(/[()]/g, " ").split(/\s+/)) {
        if (w.length > 2 && !TENDER_STOP.has(w)) tokens.add(w);
      }
    }
    if (!tokens.size) return [];
    const pool = await tendersFor(lgd);
    if (!pool.length) return [];

    // Scored on the work description alone. The location field is the body's own name,
    // identical in every one of its rows, so it cannot tell one of the body's roads from
    // another: including it only added the town's name to every candidate equally.
    const hays = pool.map((t) => (t.t || "").toLowerCase());

    // The body's own name is not evidence about which of its roads this is, and it turns
    // up in some work titles as well as in every location, so counting alone will not
    // remove it. Krishnamurtipuram, Mysuru matched a Mysuru water-supply contract purely
    // on the word Mysuru.
    const bodyWords = new Set();
    for (const w of (pool[0].loc || "").toLowerCase().split(/[^a-z]+/)) {
      if (w.length > 2) bodyWords.add(w);
    }
    for (const w of bodyWords) tokens.delete(w);
    if (!tokens.size) return [];

    const idf = new Map();
    for (const tok of tokens) {
      let df = 0;
      for (const hay of hays) if (hay.includes(tok)) df++;
      // A word in none of this body's contracts is no evidence, and a word in every one
      // of them cannot distinguish one road from another. The "more than half" cut only
      // means something once there are enough contracts to count: a town with three had
      // every matching word exceed half, so it could never match anything at all.
      if (df === 0) continue;
      if (pool.length > 1 && df === pool.length) continue;
      if (pool.length >= 8 && df > pool.length * 0.5) continue;
      idf.set(tok, Math.log((pool.length + 1) / (df + 0.5)));
    }
    if (!idf.size) return [];

    const scored = [];
    for (let i = 0; i < pool.length; i++) {
      let score = 0;
      for (const [tok, w] of idf) if (hays[i].includes(tok)) score += w;
      if (score > 0) scored.push({ score, t: pool[i] });
    }
    // Deterministic all the way down. Scores tie often, because a locality word may be the
    // only thing that matched and every ward contract for that locality then scores the
    // same: measured, all ten HSR Layout candidates tied at exactly 5.756. Without an
    // explicit order the list arrived differently on different runs and the same pothole
    // was reported under different contracts. Ties break by most recent first, since a
    // newer award is likelier to still carry an obligation, then by tender number.
    const stamp = (t) => {
      const m = /^(\d{2})-(\d{2})-(\d{4})$/.exec(String(t.d || "").trim());
      return m ? Date.UTC(+m[3], +m[2] - 1, +m[1]) : 0;
    };
    scored.sort((a, b) =>
      (b.score - a.score) || (stamp(b.t) - stamp(a.t)) || String(a.t.tn).localeCompare(String(b.t.tn)));
    return scored.slice(0, TENDER_CONFIG.maxCandidates)
      .map((x) => ({ score: x.score, tn: x.t.tn, t: x.t }));
  }

  async function tenderFromService(lat, lng, address, lgd, clientObservationId) {
    if (!finiteCoord(lat) || !finiteCoord(lng)) return { reached: false, tender: null };
    try {
      const request = { lat, lng };
      if (address) request.address_hint = address;
      if (lgd) request.lgd_hint = String(lgd);
      // Terminal resolutions are cached by the service, while a retryable 503 is not.
      // Scope the stable retry key to this observation: a future report at identical
      // coordinates must still be able to receive newer tender data.
      const logicalOperation = String(clientObservationId || randomId());
      const tenderIdempotencyKey = `tender-${await sha256HexText(logicalOperation)}`;
      const result = await signedServicePost("/v1/tenders/resolve", request,
        { idempotencyKey: tenderIdempotencyKey,
          fallback: "The central tender service is unavailable." });
      const resolution = {
        reached: true,
        jurisdiction: result.jurisdiction || null,
        request_id: result.request_id || null,
        reason: result.reason || null,
      };
      if (!result.jurisdiction
          || result.jurisdiction.road_ownership !== "municipal") {
        return { ...resolution, tender: null };
      }
      if (!result.tender) return { ...resolution, tender: null };
      const t = result.tender;
      const inferredWarranty = warrantyFor(t.published);
      return {
        ...resolution,
        tender: {
          tender_number: t.tender_number,
          contractor: t.contractor || null,
          title: t.title || "",
          published: t.published || "",
          warranty: inferredWarranty.warranty,
          warranty_code: inferredWarranty.warranty_code,
          confidence: t.confidence,
          match_method: t.match_method || "model_adjudicated",
          note: t.contractor
            ? `Probable contract: ${t.tender_number}, ${t.contractor}, published ${t.published || "date unavailable"}`
            : `Probable contract: ${t.tender_number}, contractor not listed, published ${t.published || "date unavailable"}`,
        },
      };
    } catch (error) {
      return { reached: false, tender: null, error };
    }
  }

  async function matchTender(address, lgd, lat, lng, clientObservationId) {
    // Shared mode uses the central resolver. Personal standalone mode keeps the
    // body-scoped local matcher so email preparation does not depend on that server.
    if (usingSharedVision() && finiteCoord(lat) && finiteCoord(lng)) {
      const central = await tenderFromService(lat, lng, address, lgd, clientObservationId);
      if (central.reached) return central.tender;
      if (usingSharedVision()) return null;
    }
    if (!address || !S.key || !lgd) return null;
    // Only this body's own contracts are candidates. That is what makes naming one safe:
    // the officer receiving the letter awarded the work. It also rules out a contract from
    // a different town whose road name happened to match, and state PWD, panchayat and
    // irrigation contracts, which a municipal officer has no standing over.
    const ranked = await shortlistFor(address, lgd);
    if (!ranked.length) return null;
    const candidates = ranked.map((x) => x.t);
    let m;
    try {
      // Minimal effort suits a verdict on one photo. Picking one contract out of 25
      // near-identical road-works descriptions is the opposite job, and it names a
      // real contractor in a complaint, so this call keeps room to think.
      m = await oai(buildTenderMatchRequest(address, candidates));
    } catch (e) { return null; }
    if (!m || m.match_index === null || m.match_index < 0
        || m.match_index >= candidates.length
        || m.confidence < TENDER_CONFIG.minimumConfidence) return null;
    const t = candidates[m.match_index];
    const { warranty, warranty_code } = warrantyFor(t.d);
    // Records without a winner are common in this dataset. Naming nobody is correct;
    // a placeholder sentence read as a person's name in the Kannada draft.
    const contractor = t.c || null;
    return {
      tender_number: t.tn, contractor, title: t.t, published: t.d, warranty, warranty_code,
      note: contractor
        ? `Probable contract: ${t.tn}, ${contractor}, published ${t.d}`
        : `Probable contract: ${t.tn}, contractor not listed, published ${t.d}`,
    };
  }

  function buildTenderMatchRequest(address, candidates) {
    const limits = TENDER_CONFIG.stringLimits;
    const data = {
      reverse_geocoded_address: String(address || "").slice(0, limits.address),
      candidates: (Array.isArray(candidates) ? candidates : [])
        .slice(0, TENDER_CONFIG.maxCandidates).map((t, index) => ({
        match_index: index,
        work_description: String(t && t.t || "").slice(0, limits.workDescription),
        division_or_location: String(t && t.loc || "").slice(0, limits.divisionOrLocation),
        contractor: String(t && t.c || "not named").slice(0, limits.contractor),
        published: String(t && t.d || "").slice(0, limits.published),
      })),
    };
    const envelope = `${TENDER_PROMPT_CONFIG.dataEnvelope.begin}\n${JSON.stringify(data)}\n${TENDER_PROMPT_CONFIG.dataEnvelope.end}`;
    return {
      model: TENDER_CONFIG.model,
      instructions: TENDER_MATCH_INSTRUCTIONS,
      input: [{
        role: TENDER_PROMPT_CONFIG.dataRole,
        content: [{ type: "input_text", text: envelope }],
      }],
      reasoning: { effort: TENDER_CONFIG.reasoningEffort },
      text: fmt(TENDER_PROMPT_CONFIG.schemaName, TENDER_SCHEMA),
    };
  }

  // ---------- drafting (English / Kannada) ----------
  function damageTypeOf(value) {
    if (value && value.damage_type) return value.damage_type;
    return value && value.is_pothole ? "pothole_cavity" : null;
  }

  function assessmentOf(value) {
    const assessment = value && value.assessment;
    if (assessment === "damaged" || assessment === "undamaged") return assessment;
    if (assessment === "clear" || assessment === "probable") return "damaged";
    if (assessment === "absent") return "undamaged";
    return value && (value.decision === "accept" || value.is_pothole) ? "damaged" : "undamaged";
  }

  function draftEmail(a, lat, lng, address, officerName, tender) {
    const kn = LANG() === "kn";
    const sizeName = (s) => (kn ? ({ small: "ಸಣ್ಣ", medium: "ಮಧ್ಯಮ", large: "ದೊಡ್ಡ" })[s] || s : s);
    const size = a.size ? sizeName(a.size) : (kn ? "ಗಾತ್ರ ನಿರ್ಧರಿಸದ" : "unclassified");
    const road = address ? address.split(",")[0].trim() : null;
    const type = damageTypeOf(a);
    const typeNames = kn ? {
      pothole_cavity: "ರಸ್ತೆ ಗುಂಡಿ", failed_patch: "ವಿಫಲವಾದ ರಸ್ತೆ ದುರಸ್ತಿ",
      surface_breakup: "ಹಾಳಾದ ರಸ್ತೆ ಮೇಲ್ಮೈ", rut_or_depression: "ರಸ್ತೆ ಕುಸಿತ",
      other_road_damage: "ರಸ್ತೆ ಹಾನಿ", none: "ರಸ್ತೆ ಹಾನಿ",
    } : {
      pothole_cavity: "pothole", failed_patch: "failed road repair",
      surface_breakup: "broken road surface", rut_or_depression: "road rut or depression",
      other_road_damage: "road damage", none: "road damage",
    };
    const typeName = typeNames[type] || typeNames.other_road_damage;

    let locLines;
    if (lat != null) {
      const la = lat.toFixed(6), ln = lng.toFixed(6);
      locLines = kn
        ? `ಸ್ಥಳ: ${address || "ಕೆಳಗಿನ ನಿರ್ದೇಶಾಂಕ ನೋಡಿ"}\nನಿರ್ದೇಶಾಂಕಗಳು: ${la}, ${ln}\nನಕ್ಷೆ ಲಿಂಕ್: https://maps.google.com/?q=${la},${ln}`
        : `Location: ${address || "see coordinates below"}\nCoordinates: ${la}, ${ln}\nMap link: https://maps.google.com/?q=${la},${ln}`;
    } else {
      locLines = kn
        ? "ಸ್ಥಳ: ಸ್ವಯಂಚಾಲಿತವಾಗಿ ನಿರ್ಧರಿಸಲಾಗಲಿಲ್ಲ. ದಯವಿಟ್ಟು ಲಗತ್ತಿಸಿದ ಫೋಟೋ ನೋಡಿ."
        : "Location: could not be determined automatically. Please see the attached photo for landmarks.";
    }

    const subject = kn
      ? `${typeName} ದೂರು` + (type === "pothole_cavity" ? `: ${size}` : "") + (road ? ` (${road})` : "")
      : `${type === "pothole_cavity" ? `Pothole complaint: ${size} pothole`
          : type === "failed_patch" ? "Broken road repair complaint"
          : type === "surface_breakup" ? "Road surface failure complaint"
          : type === "rut_or_depression" ? "Road depression complaint"
          : "Road damage complaint"}` + (road ? ` near ${road}` : "");

    const observedDetails = String(a.description || "").replace(/\s+/g, " ").trim().slice(0, 500);
    const paras = kn
      ? [
          `ಮಾನ್ಯ ${officerName || "ಅಧಿಕಾರಿಗಳೇ"} ಅವರಿಗೆ,`,
          `ದುರಸ್ತಿ ಅಗತ್ಯವಿರುವ ${typeName} ಬಗ್ಗೆ ದೂರು ಸಲ್ಲಿಸುತ್ತಿದ್ದೇನೆ.`,
          `${locLines}\nಹಾನಿಯ ಪ್ರಕಾರ: ${typeName}${a.size ? `\nಅಂದಾಜು ಗಾತ್ರ: ${size}` : ""}${observedDetails ? `\nಗಮನಿಸಿದ ವಿವರಗಳು: ${observedDetails}` : ""}`,
          "ಫೋಟೋ ಲಗತ್ತಿಸಲಾಗಿದೆ. ಈ ರಸ್ತೆ ಹಾನಿ ದ್ವಿಚಕ್ರ ವಾಹನ ಸವಾರರಿಗೆ ಮತ್ತು ಇತರ ರಸ್ತೆ ಬಳಕೆದಾರರಿಗೆ ಅಪಾಯಕಾರಿ. ಇದನ್ನು ಶೀಘ್ರ ಪರಿಶೀಲಿಸಿ ದುರಸ್ತಿ ಮಾಡಬೇಕೆಂದು ವಿನಂತಿಸುತ್ತೇನೆ.",
        ]
      : [
          `Dear ${officerName || "Sir or Madam"},`,
          `I would like to report a ${typeName} that needs repair.`,
          `${locLines}\nDamage type: ${typeName}${a.size ? `\nApproximate size: ${size}` : ""}${observedDetails ? `\nObserved details: ${observedDetails}` : ""}`,
          "PFA image. This road damage poses a danger to two wheeler riders and other road users. I request your office to inspect and repair it at the earliest.",
        ];

    const tenderNumber = String(tender && tender.tender_number || "").trim();
    if (tenderNumber) {
      const title = String(tender.title || "").slice(0, 140).trim();
      const published = String(tender.published || "").trim();
      // Two paragraphs, not one: the first states what the records say, the second makes
      // the request. Published, never "awarded": the bundled field is the publication
      // date, and this letter names a real company to a government officer.
      if (kn) {
        paras.push(`ಸಾರ್ವಜನಿಕ ಖರೀದಿ ದಾಖಲೆಗಳ ಪ್ರಕಾರ ಈ ರಸ್ತೆ ಭಾಗ ಟೆಂಡರ್ ${tenderNumber}${title ? ` ("${title}")` : ""} ಅಡಿಯಲ್ಲಿ ಬರುವ ಸಾಧ್ಯತೆ ಇದೆ.${published ? ` ಇದು ${published} ರಂದು ಪ್ರಕಟವಾಗಿದೆ` : ""}${tender.contractor ? `${published ? "," : ""} ಗೆದ್ದ ಬಿಡ್‌ದಾರರಾಗಿ ${tender.contractor} ಎಂದು ದಾಖಲಾಗಿದೆ` : ""}.`);
        paras.push("ಇದು ಸಂಭಾವ್ಯ ದಾಖಲೆ ಹೊಂದಾಣಿಕೆ ಮಾತ್ರ. ಪ್ರಕಟಣೆ ದಿನಾಂಕವು ಈ ಗುತ್ತಿಗೆದಾರರಿಗೆ ಪ್ರಸ್ತುತ ದೋಷ ಹೊಣೆಗಾರಿಕೆ ಅಥವಾ ನಿರ್ವಹಣಾ ಬಾಧ್ಯತೆ ಇದೆ ಎಂದು ಸ್ಥಾಪಿಸುವುದಿಲ್ಲ. ದಯವಿಟ್ಟು ಟೆಂಡರ್, ಗುತ್ತಿಗೆ ಮಂಜೂರಾತಿ, ಕಾರ್ಯಾದೇಶ ಮತ್ತು ಅನ್ವಯಿಸುವ ಹೊಣೆಗಾರಿಕೆ ಅಥವಾ ನಿರ್ವಹಣಾ ಷರತ್ತುಗಳನ್ನು ಪರಿಶೀಲಿಸಿ, ಹೊಣೆಗಾರ ಪಕ್ಷದ ಮೂಲಕ ದುರಸ್ತಿ ಮಾಡಿಸಲು ವಿನಂತಿಸುತ್ತೇನೆ.");
      } else {
        paras.push(`Public procurement records indicate this road stretch probably falls under tender ${tenderNumber}${title ? ` ("${title}")` : ""}${published ? `, published on ${published}` : ""}${tender.contractor ? `, with ${tender.contractor} recorded as the winning bidder` : ""}.`);
        paras.push("This is only a probable record match. The publication date does not establish that this contractor has any current defect-liability or maintenance obligation. Kindly verify the tender, award, work order and applicable liability or maintenance terms, and arrange repair through the responsible party.");
      }
    }

    paras.push(kn ? "ನಿಮ್ಮ ಸೇವೆಗೆ ಧನ್ಯವಾದಗಳು." : "Thank you for your service.");
    paras.push(kn ? `ವಂದನೆಗಳು,\n${S.name}` : `Regards,\n${S.name}`);
    return [subject, paras.join("\n\n")];
  }

  // ---------- storage (IndexedDB) ----------
  let _db = null;
  function idb() {
    return new Promise((resolve, reject) => {
      if (_db) return resolve(_db);
      const req = indexedDB.open("potholes", 8);
      req.onupgradeneeded = (event) => {
        const d = req.result;
        const reports = d.objectStoreNames.contains("reports")
          ? req.transaction.objectStore("reports")
          : d.createObjectStore("reports", { keyPath: "id", autoIncrement: true });
        // Cursor over lightweight candidate ranges instead of getAll(): report records
        // contain photos, so cloning every old image for every accepted frame would make
        // long footage analysis slower and more memory-hungry as history grows.
        if (!reports.indexNames.contains("by_lat")) reports.createIndex("by_lat", "lat");
        if (!reports.indexNames.contains("by_drive")) reports.createIndex("by_drive", "drive_id");
        // A canonical event can be observed on later drives without changing its original
        // drive_id. Index every drive that has seen it so the next adjacent observation is
        // found even when it lies outside the stricter cross-drive radius or has no GPS.
        if (!reports.indexNames.contains("by_sighting_drive")) {
          reports.createIndex("by_sighting_drive", "sighting_drive_ids", { multiEntry: true });
        }
        // How many frames a drive actually checked is only known while it runs:
        // rejected frames are not kept unless debug mode is on, so the count has to
        // be recorded at the end or it is lost.
        if (!d.objectStoreNames.contains("drives")) d.createObjectStore("drives", { keyPath: "id" });
        // Continuous footage: capture stops guessing an interval, and a drive can be
        // re-analysed later, more densely or by a better model. Discarded frames are gone.
        if (!d.objectStoreNames.contains("footage")) {
          const f = d.createObjectStore("footage", { keyPath: "key" });
          f.createIndex("by_drive", "drive_id");
        }
        if (!d.objectStoreNames.contains("identity")) {
          d.createObjectStore("identity", { keyPath: "key" });
        }
        // Only the small central observation body is queued--never a photo, complaint,
        // name, or API key. The observation ID is both its key and the server's stable
        // idempotency key, so reconnecting cannot create a second sighting.
        const centralOutbox = d.objectStoreNames.contains("central_outbox")
          ? req.transaction.objectStore("central_outbox")
          : d.createObjectStore("central_outbox", { keyPath: "client_observation_id" });
        if (!centralOutbox.indexNames.contains("by_report")) {
          centralOutbox.createIndex("by_report", "report_id");
        }
        // Builds before v8 could label a road "municipal" after checking only one road
        // layer. That bare value is not proof that the central ownership resolver checked
        // NH/SH/DH, so no cached recipient, contractor, or draft from those builds may be
        // reopened. Preserve the observation/evidence and force one central revalidation.
        if (event.oldVersion > 0 && event.oldVersion < 8) {
          const cursorRequest = reports.openCursor();
          cursorRequest.onsuccess = () => {
            const cursor = cursorRequest.result;
            if (!cursor) return;
            const rec = cursor.value;
            const accepted = rec.decision === "accept"
              || ["draft", "queued", "sent", "unrouted", "duplicate"].includes(rec.status);
            // Both legacy detector modes used phone-side civic/tender routing. The
            // project resolver is now authoritative for both, so neither old Personal
            // nor old Shared attributions are safe to display or email without a retry.
            if (accepted) {
              for (const field of [
                "address", "body_lgd", "body_name", "road_ownership",
                "road_ownership_source",
                "road_ownership_detail", "officer_name", "officer_email", "officer_title",
                "email_to", "email_subject", "email_body", "email_opened_at", "sent_at",
                "tender_number", "contractor", "tender_note", "tender_title",
                "tender_published", "tender_resolution_reason",
                "tender_resolution_checked_at", "unrouted_reason", "unrouted_body",
              ]) rec[field] = null;
              rec.status = rec.status === "duplicate" || rec.server_duplicate
                ? "duplicate" : "draft";
              cursor.update(rec);
            }
            cursor.continue();
          };
        }
      };
      req.onsuccess = () => { _db = req.result; resolve(_db); };
      req.onerror = () => reject(req.error);
    });
  }
  // A write is not done when the request succeeds, it is done when the transaction
  // commits. Chrome reports a full disk by aborting the transaction, and the request
  // itself still succeeds, so resolving on req.onsuccess reported success for writes that
  // rolled back: measured, 672 MB of footage reported stored and absent afterwards. A
  // read has nothing to commit, so it still resolves on the request.
  function op(mode, fn, storeName = "reports") {
    return idb().then((d) => new Promise((resolve, reject) => {
      const tx = d.transaction(storeName, mode);
      const req = fn(tx.objectStore(storeName));
      if (mode === "readonly") {
        req.onsuccess = () => resolve(req.result);
        req.onerror = () => reject(req.error);
        return;
      }
      let value, failure = null;
      req.onsuccess = () => { value = req.result; };
      req.onerror = (e) => { failure = req.error; e.preventDefault(); };
      tx.oncomplete = () => resolve(value);
      const died = () => reject(storageError(failure || tx.error));
      tx.onabort = died;
      tx.onerror = died;
    }));
  }

  // A full device is the common cause and the only one the user can act on, so it says so
  // rather than surfacing a DOMException name.
  function storageError(err) {
    const name = err && err.name;
    if (name === "QuotaExceededError") {
      return new Error("This phone is out of storage, so nothing more can be saved. Free some space, or delete old drives and their video from the app.");
    }
    return new Error((err && err.message) || "Could not save to this device's storage.");
  }
  const allReports = () => op("readonly", (s) => s.getAll());
  const getReport = (id) => op("readonly", (s) => s.get(Number(id)));
  const putReport = (r) => op("readwrite", (s) => s.put(r));
  const addReport = (r) => op("readwrite", (s) => s.add(r));
  const allCentralOutbox = () => op("readonly", (s) => s.getAll(), "central_outbox");
  const delCentralOutbox = (id) => op("readwrite", (s) => s.delete(String(id)), "central_outbox");
  const allDrives = () => op("readonly", (s) => s.getAll(), "drives");
  const getDrive = (id) => op("readonly", (s) => s.get(String(id)), "drives");
  const putFootage = (seg) => op("readwrite", (s) => s.put(seg), "footage");
  const footageFor = (driveId) => op("readonly", (s) => s.index("by_drive").getAll(String(driveId)), "footage");
  const getFootage = (key) => op("readonly", (s) => s.get(String(key)), "footage");
  const putDrive = (d) => op("readwrite", (s) => s.put(d), "drives");

  // IndexedDB's getAll() clones every Blob in the result. A long drive can therefore
  // exhaust the WebView just by opening History, before analysis has decoded one frame.
  // Walk the store and retain metadata only; at most the cursor's current Blob is live.
  function footageMetadata(driveId = null) {
    return idb().then((d) => new Promise((resolve, reject) => {
      const tx = d.transaction("footage", "readonly");
      const store = tx.objectStore("footage");
      const req = driveId == null
        ? store.openCursor()
        : store.index("by_drive").openCursor(IDBKeyRange.only(String(driveId)));
      const rows = [];
      req.onsuccess = () => {
        const cursor = req.result;
        if (!cursor) return;
        const f = cursor.value;
        rows.push({
          key: String(f.key),
          drive_id: String(f.drive_id),
          seq: Number.isFinite(f.seq) ? f.seq : 0,
          mime: f.mime || f.blob && f.blob.type || "video/mp4",
          bytes: Number.isFinite(f.bytes) ? f.bytes : f.blob && f.blob.size || 0,
          recording_started_at_ms: Number.isFinite(f.recording_started_at_ms)
            ? f.recording_started_at_ms : null,
          source_offset_s: Number.isFinite(f.source_offset_s) ? f.source_offset_s : null,
          at: Number.isFinite(f.at) ? f.at : null,
        });
        cursor.continue();
      };
      req.onerror = () => reject(req.error);
      tx.oncomplete = () => resolve(rows);
      tx.onabort = () => reject(tx.error || new Error("Could not read stored footage."));
    }));
  }

  function deleteFootageFor(driveId) {
    return idb().then((d) => new Promise((resolve, reject) => {
      const tx = d.transaction("footage", "readwrite");
      const req = tx.objectStore("footage").index("by_drive")
        .openCursor(IDBKeyRange.only(String(driveId)));
      let failure = null;
      req.onsuccess = () => {
        const cursor = req.result;
        if (!cursor) return;
        const del = cursor.delete();
        del.onerror = () => { failure = del.error; };
        cursor.continue();
      };
      req.onerror = () => { failure = req.error; };
      tx.oncomplete = () => resolve();
      const died = () => reject(storageError(failure || tx.error));
      tx.onabort = died;
      tx.onerror = died;
    }));
  }

  // Accepted Drive jobs finish concurrently. A separate getAll() followed by add()
  // lets two nearby jobs both observe "none" and both write. Keep the final check and
  // insert in one read-write transaction; IndexedDB serialises these transactions on the
  // reports store, so exactly one concurrent detection becomes the saved event.
  function addReportUnlessDuplicate(rec, dedupe, centralOutboxRow = null) {
    return idb().then((d) => new Promise((resolve, reject) => {
      const tx = d.transaction(centralOutboxRow
        ? ["reports", "central_outbox"] : ["reports"], "readwrite");
      const store = tx.objectStore("reports");
      const outbox = centralOutboxRow ? tx.objectStore("central_outbox") : null;
      let result = null, failure = null;
      const queueCentral = (reportId, mergedIntoExisting) => {
        if (!outbox) return;
        const queued = outbox.put({
          ...centralOutboxRow,
          report_id: Number(reportId),
          merged_into_existing: !!mergedIntoExisting,
        });
        queued.onerror = () => { failure = queued.error; };
      };
      const addNew = () => {
        const add = store.add(rec);
        add.onsuccess = () => {
          result = { id: add.result, duplicate: null };
          queueCentral(add.result, false);
        };
        add.onerror = () => { failure = add.error; };
      };
      const scan = (request, next) => {
        request.onsuccess = () => {
          const cursor = request.result;
          if (!cursor) { next(); return; }
          const match = roadEventMatch(rec, cursor.value);
          if (match) {
            const prior = cursor.value;
            const keys = Array.isArray(prior.source_event_keys)
              ? prior.source_event_keys.slice() : (prior.source_event_key ? [prior.source_event_key] : []);
            if (rec.source_event_key && !keys.includes(rec.source_event_key)) keys.push(rec.source_event_key);
            const exactReplay = match.kind === "same_source";
            const observedAt = eventTime(rec);
            // Keep a bounded envelope per drive, not a global 64-item cap: popular
            // locations can be revisited many times, and a full old global array would
            // otherwise stop recording the first sighting of a new drive. Sightings older
            // than the cross-drive horizon are no longer useful for approximate matching;
            // exact retained-footage replays remain covered by source_event_keys.
            const currentDrive = rec.drive_id == null ? null : String(rec.drive_id);
            const cutoff = Number.isFinite(observedAt) ? observedAt - DEDUPE_HISTORY_S : -Infinity;
            const sightings = storedSightings(prior).filter((seen) => {
              const seenAt = Number.isFinite(seen.captured_at) ? seen.captured_at : null;
              return (seen.drive_id != null && String(seen.drive_id) === currentDrive)
                || seenAt == null || seenAt >= cutoff;
            });
            const sameDriveCount = currentDrive == null ? 0 : sightings.filter((seen) =>
              seen.drive_id != null && String(seen.drive_id) === currentDrive).length;
            if ((match.kind === "same_drive" || match.kind === "prior_drive")
                && !exactReplay && (currentDrive == null || sameDriveCount < 64)) {
              sightings.push(eventSighting(rec));
            }
            const sightingDriveIds = [...new Set(sightings
              .map((seen) => seen.drive_id == null ? null : String(seen.drive_id)).filter(Boolean))];
            const updated = {
              ...prior,
              source_event_keys: keys.slice(-64),
              event_sightings: sightings,
              sighting_drive_ids: sightingDriveIds,
              seen_count: exactReplay ? (prior.seen_count || 1) : (prior.seen_count || 1) + 1,
              last_seen_at: Math.max(eventTime(prior) || 0, eventTime(rec) || 0),
            };
            if (centralOutboxRow) {
              updated.central_sync_pending = true;
              updated.server_sync_error = rec.server_sync_error || updated.server_sync_error || null;
              updated.server_request_id = rec.server_request_id || updated.server_request_id || null;
            }
            const write = cursor.update(updated);
            write.onsuccess = () => {
              result = { id: null, duplicate: updated, match: match.kind };
              queueCentral(prior.id, true);
            };
            write.onerror = () => { failure = write.error; };
            return;
          }
          cursor.continue();
        };
        request.onerror = () => { failure = request.error; };
      };
      const scanLocation = () => {
        if (!finiteCoord(rec.lat) || !finiteCoord(rec.lng)) { addNew(); return; }
        const latitudeBand = DEDUPE_HISTORY_RADIUS_M / 110900;
        scan(store.index("by_lat").openCursor(
          IDBKeyRange.bound(rec.lat - latitudeBand, rec.lat + latitudeBand)), addNew);
      };
      try {
        if (!dedupe) addNew();
        else if (rec.drive_id != null) {
          const driveKey = String(rec.drive_id);
          const scanOriginalDrive = () => scan(
            store.index("by_drive").openCursor(IDBKeyRange.only(driveKey)), scanLocation);
          scan(store.index("by_sighting_drive").openCursor(IDBKeyRange.only(driveKey)), scanOriginalDrive);
        } else scanLocation();
      } catch (e) {
        failure = e;
        try { tx.abort(); } catch (_) {}
      }
      tx.oncomplete = () => result ? resolve(result)
        : reject(storageError(failure || new Error("Could not save this report.")));
      const died = () => reject(storageError(failure || tx.error));
      tx.onabort = died;
      tx.onerror = () => {};
    }));
  }

  // Delete the evidence and every not-yet-delivered central observation that belongs to
  // it in one transaction. A reconnect racing this action can finish before it or after
  // it, but it cannot resurrect a report the user deleted.
  function deleteReportAndCentralOutbox(id) {
    const reportId = Number(id);
    return idb().then((d) => new Promise((resolve, reject) => {
      const tx = d.transaction(["reports", "central_outbox"], "readwrite");
      tx.objectStore("reports").delete(reportId);
      const cursorRequest = tx.objectStore("central_outbox").index("by_report")
        .openCursor(IDBKeyRange.only(reportId));
      cursorRequest.onsuccess = () => {
        const cursor = cursorRequest.result;
        if (!cursor) return;
        cursor.delete();
        cursor.continue();
      };
      tx.oncomplete = () => resolve();
      const died = () => reject(storageError(tx.error));
      tx.onabort = died;
      tx.onerror = () => {};
    }));
  }

  // Photos are stored as blobs, not base64. Measured on a device with a hundred 1024px
  // thumbnails: reading them back took 177 ms as base64 strings and 3 ms as blobs, writing
  // took 253 ms against 90 ms, and each one is 88 KB as text against 66 KB binary. Every
  // screen that lists reports paid that difference, which is why the app felt slow
  // everywhere rather than in one place.
  //
  // Records written before this change hold a data URL string. Everything that reads a
  // photo accepts either, so nothing has to be migrated or rewritten.
  const dataUrlToBlob = async (u) => {
    if (!u || typeof u !== "string") return u || null;
    try { return await (await fetch(u)).blob(); } catch (e) { return u; }
  };
  async function imageHash(dataUrl) {
    const value = String(dataUrl || "");
    const comma = value.indexOf(",");
    if (comma < 0) return sha256HexText(value);
    const meta = value.slice(0, comma);
    const payload = value.slice(comma + 1);
    try {
      if (/;base64$/i.test(meta)) {
        const binary = atob(payload);
        const bytes = new Uint8Array(binary.length);
        for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
        return sha256HexBytes(bytes);
      }
      return sha256HexBytes(new TextEncoder().encode(decodeURIComponent(payload)));
    } catch (_) {
      return sha256HexText(value);
    }
  }
  const photoToBase64 = async (v) => {
    if (!v) return null;
    if (typeof v === "string") return v.split(",")[1];
    return await new Promise((res) => {
      const fr = new FileReader();
      fr.onload = () => res(String(fr.result).split(",")[1]);
      fr.onerror = () => res(null);
      fr.readAsDataURL(v);
    });
  };

  // Older builds used "sent" after merely opening the mail composer. Preserve those
  // records, but never present that unverified state as successful delivery.
  const publicEmailStatus = (status) => status === "sent" ? "queued" : status;
  const toDict = (r) => ({ ...r, status: publicEmailStatus(r.status), photo_url: r.photo });
  // The list never renders the evidence copy, so it never receives it.
  const listDict = (r) => { const d = toDict(r); delete d.photo_full; return d; };

  function applyCentralPothole(rec, response) {
    const pothole = response && response.pothole;
    if (!pothole || !pothole.id) return rec;
    rec.server_pothole_id = String(pothole.id);
    rec.server_request_id = response.request_id || rec.server_request_id || null;
    rec.server_seen_count = Number.isFinite(pothole.seen_count) ? pothole.seen_count
      : (rec.server_seen_count || null);
    rec.seen_count = Math.max(rec.seen_count || 1, pothole.seen_count || 1);
    rec.server_first_seen_at = pothole.first_seen_at || rec.server_first_seen_at || null;
    rec.server_last_seen_at = pothole.last_seen_at || rec.server_last_seen_at || null;
    rec.canonical_lat = finiteCoord(pothole.lat) ? pothole.lat : rec.canonical_lat;
    rec.canonical_lng = finiteCoord(pothole.lng) ? pothole.lng : rec.canonical_lng;
    rec.body_lgd = pothole.lgd || rec.body_lgd || null;
    rec.body_name = pothole.town || rec.body_name || null;
    return rec;
  }

  function centralReportIsConfirmed(rec) {
    return !!rec && Number(rec.server_pothole_id) > 0
      && !rec.central_sync_pending
      && !rec.server_duplicate && rec.status !== "duplicate";
  }

  async function centralPotholeRequest(
    rec, workingDataUrl, detector, detectionReceipt, tenderResolution,
  ) {
    if (!finiteCoord(rec.lat) || !finiteCoord(rec.lng)) return null;
    // Only the resolver response owned by this observation may provide authority
    // hints. A process-wide same-coordinate cache can be overwritten by another
    // capture while this one is still awaiting image hashing or report upload.
    const jurisdiction = tenderResolution && tenderResolution.reached
      ? tenderResolution.jurisdiction : null;
    const observedAt = Math.round((Number.isFinite(rec.captured_at)
      ? rec.captured_at : rec.created_at) * 1000);
    const request = {
      client_observation_id: rec.client_observation_id,
      observed_at: observedAt,
      lat: rec.lat,
      lng: rec.lng,
      gps_accuracy_m: Number.isFinite(rec.gps_accuracy) ? rec.gps_accuracy : null,
      heading_deg: Number.isFinite(rec.heading) ? rec.heading : null,
      speed_mps: Number.isFinite(rec.speed_mps) ? rec.speed_mps : null,
      damage_type: rec.damage_type,
      size: rec.size || null,
      image_hash: await imageHash(workingDataUrl),
      detector: detector || {},
    };
    if (detectionReceipt) request.detection_receipt = String(detectionReceipt);
    const lgd = jurisdiction && jurisdiction.lgd;
    const town = jurisdiction && jurisdiction.town;
    if (lgd) request.lgd_hint = String(lgd);
    if (town) request.town_hint = String(town);
    return request;
  }

  async function registerCentralPothole(request, exactBody) {
    if (!request) return null;
    return signedServicePost("/v1/potholes/report", request, {
      idempotencyKey: request.client_observation_id,
      exactBody,
      fallback: "The pothole could not be added to the shared map.",
    });
  }

  function recordCentralRetryFailure(queued, error) {
    const key = String(queued.client_observation_id);
    return idb().then((d) => new Promise((resolve, reject) => {
      const tx = d.transaction(["reports", "central_outbox"], "readwrite");
      const reports = tx.objectStore("reports");
      const outbox = tx.objectStore("central_outbox");
      const getQueued = outbox.get(key);
      getQueued.onsuccess = () => {
        const current = getQueued.result;
        // Deletion may have removed this operation while its network request was in
        // flight. Never put an obsolete queue row (or its report) back.
        if (!current) return;
        current.attempt_count = (current.attempt_count || 0) + 1;
        current.last_attempt_at = Date.now();
        current.last_error = error && error.message || "Shared-map sync failed.";
        current.last_request_id = error && error.requestId || null;
        outbox.put(current);
        const getReportRequest = reports.get(Number(current.report_id));
        getReportRequest.onsuccess = () => {
          const rec = getReportRequest.result;
          if (!rec) return;
          rec.central_sync_pending = true;
          rec.server_sync_error = current.last_error;
          rec.server_request_id = current.last_request_id || rec.server_request_id || null;
          reports.put(rec);
        };
      };
      tx.oncomplete = () => resolve();
      const died = () => reject(storageError(tx.error));
      tx.onabort = died;
      tx.onerror = () => {};
    }));
  }

  function completeCentralRetry(queued, response) {
    const key = String(queued.client_observation_id);
    return idb().then((d) => new Promise((resolve, reject) => {
      const tx = d.transaction(["reports", "central_outbox"], "readwrite");
      const reports = tx.objectStore("reports");
      const outbox = tx.objectStore("central_outbox");
      const getQueued = outbox.get(key);
      getQueued.onsuccess = () => {
        const current = getQueued.result;
        if (!current) return;
        const reportId = Number(current.report_id);
        const getReportRequest = reports.get(reportId);
        getReportRequest.onsuccess = () => {
          const rec = getReportRequest.result;
          if (!rec) {
            outbox.delete(key);
            return;
          }
          const serverId = response && response.pothole && response.pothole.id;
          const sameCanonical = !rec.server_pothole_id || !serverId
            || String(rec.server_pothole_id) === String(serverId);
          // An automatic frame can be merged into an already-saved local canonical.
          // Its retry is another sighting, not evidence that the canonical complaint
          // itself was a duplicate. Only copy server state when both identify the same
          // canonical (or the local record had not yet been linked).
          if (!current.merged_into_existing || sameCanonical) {
            applyCentralPothole(rec, response);
          } else {
            rec.server_request_id = response.request_id || rec.server_request_id || null;
          }
          if (!current.merged_into_existing) {
            rec.server_duplicate = !!response.duplicate;
            rec.server_dedupe_distance_m = response.dedupe
              && Number.isFinite(response.dedupe.distance_m) ? response.dedupe.distance_m : null;
            if (response.duplicate) {
              rec.duplicate = true;
              rec.duplicate_of = rec.server_pothole_id || (serverId && String(serverId)) || null;
              // A user might have opened the draft before connectivity returned.
              // Preserve that history, but do not claim the email was delivered.
              if (rec.status === "sent") rec.status = "queued";
              if (rec.status !== "queued") {
                rec.status = "duplicate";
                rec.email_subject = null;
                rec.email_body = null;
              }
            }
          }
          outbox.delete(key);
          const remaining = outbox.index("by_report").count(IDBKeyRange.only(reportId));
          remaining.onsuccess = () => {
            rec.central_sync_pending = remaining.result > 0;
            if (!rec.central_sync_pending) {
              rec.server_sync_error = null;
            }
            reports.put(rec);
          };
        };
      };
      tx.oncomplete = () => resolve();
      const died = () => reject(storageError(tx.error));
      tx.onabort = died;
      tx.onerror = () => {};
    }));
  }

  let centralRetryPromise = null;
  function retryCentralOutbox() {
    if (centralRetryPromise) return centralRetryPromise;
    centralRetryPromise = (async () => {
      const queuedRows = (await allCentralOutbox())
        .sort((a, b) => (a.created_at || 0) - (b.created_at || 0));
      for (const queued of queuedRows) {
        // Orphans should only arise from an interrupted migration. Clearing one here is
        // safe and also preserves the rule that deleting evidence cancels pending sync.
        if (!await getReport(queued.report_id)) {
          await delCentralOutbox(queued.client_observation_id);
          continue;
        }
        let response;
        try {
          const request = JSON.parse(queued.body);
          response = await signedServicePost(queued.path || "/v1/potholes/report", request, {
            idempotencyKey: queued.client_observation_id,
            exactBody: queued.body,
            fallback: "The pothole could not be added to the shared map.",
          });
          if (!response || !response.pothole || !response.pothole.id) {
            throw new Error("The reporting service returned an incomplete pothole record.");
          }
          await completeCentralRetry(queued, response);
        } catch (error) {
          await recordCentralRetryFailure(queued, error);
          // One unreachable or overloaded service would fail every queued row. Leave the
          // remainder durable for the next startup/reconnect instead of hammering it.
          if (!error || !Number.isFinite(error.status) || error.status === 408
              || error.status === 429 || error.status >= 500) break;
        }
      }
    })().finally(() => { centralRetryPromise = null; });
    return centralRetryPromise;
  }

  async function flushCentralOutbox() {
    // A startup flush can race a report committed one tick later. Joining that empty
    // pass and then starting a fresh one guarantees the new row is not stranded.
    const prior = centralRetryPromise;
    if (prior) await prior;
    return retryCentralOutbox();
  }

  // ---------- image ----------

  const ROAD_BAND = IMAGING_CONFIG.drive.roadBand;

  function averageLuminance(ctx, width, height) {
    const data = ctx.getImageData(0, 0, width, height).data;
    const lightConfig = IMAGING_CONFIG.adaptiveLuminance;
    const step = Math.max(1, Math.floor(Math.sqrt(
      (width * height) / lightConfig.targetSamples)));
    let total = 0, count = 0, clippedDark = 0, clippedBright = 0;
    for (let y = 0; y < height; y += step) {
      for (let x = 0; x < width; x += step) {
        const i = (y * width + x) * 4;
        const lum = 0.2126 * data[i] + 0.7152 * data[i + 1] + 0.0722 * data[i + 2];
        total += lum; count++;
        if (lum < lightConfig.darkPixelThreshold) clippedDark++;
        if (lum > lightConfig.brightPixelThreshold) clippedBright++;
      }
    }
    return { mean: count ? total / count : 0,
             dark: count ? clippedDark / count : 1,
             bright: count ? clippedBright / count : 0 };
  }

  async function toDataUrl(blob, maxDim, quality = 0.85, boost = false, band = 1) {
    const bmp = await createImageBitmap(blob, { imageOrientation: "from-image" });
    const sx = 0, sw = bmp.width;
    const sh = Math.max(1, Math.round(bmp.height * band));
    const sy = bmp.height - sh;
    const scale = Math.min(1, maxDim / Math.max(sw, sh));
    const c = document.createElement("canvas");
    c.width = Math.round(sw * scale);
    c.height = Math.round(sh * scale);
    const ctx = c.getContext("2d");
    ctx.drawImage(bmp, sx, sy, sw, sh, 0, 0, c.width, c.height);
    // Enhancement follows the pixels, not the wall clock. Fixed evening hours boosted
    // bright street-lit frames and amplified noise. Preserve the original evidence copy;
    // this is only the small image used for detection.
    const light = boost ? averageLuminance(ctx, c.width, c.height) : null;
    const lightConfig = IMAGING_CONFIG.adaptiveLuminance;
    if (boost && light.mean < lightConfig.meanThreshold
        && light.bright < lightConfig.brightFractionThreshold) {
      const lift = Math.min(lightConfig.maximumLift, Math.max(lightConfig.minimumLift,
        lightConfig.targetMean / Math.max(lightConfig.meanFloor, light.mean)));
      ctx.filter = `brightness(${lift.toFixed(2)}) contrast(${lightConfig.contrast})`;
      ctx.drawImage(bmp, sx, sy, sw, sh, 0, 0, c.width, c.height);
      ctx.filter = "none";
    }
    const out = c.toDataURL("image/jpeg", quality);
    if (bmp.close) bmp.close();
    return out;
  }

  // ---------- pipeline ----------
  // Detection requests stay concurrent, but their final storage decisions must follow
  // capture order within one drive. Otherwise a later frame can finish inference first,
  // sit just outside the historical radius, and briefly become a second canonical before
  // the earlier bridging frame is known. Callers submit live/VOD frames chronologically;
  // this queue serialises only the short post-detection commit, not model inference.
  const driveCommitTails = new Map();
  function reserveDriveCommit(driveId) {
    if (driveId == null) return null;
    const key = String(driveId);
    const wait = driveCommitTails.get(key) || Promise.resolve();
    let release;
    const own = new Promise((resolve) => { release = resolve; });
    const tail = wait.then(() => own);
    driveCommitTails.set(key, tail);
    tail.finally(() => { if (driveCommitTails.get(key) === tail) driveCommitTails.delete(key); });
    let finished = false;
    return {
      wait,
      done() {
        if (finished) return;
        finished = true;
        release();
      },
    };
  }

  async function createReport(fd, driveMode) {
    const photo = fd.get("photo");
    if (!photo || !photo.size) throw new Error("Empty photo.");
    const latRaw = fd.get("lat"), lngRaw = fd.get("lng");
    const lat = latRaw != null && latRaw !== "" ? parseFloat(latRaw) : null;
    const lng = lngRaw != null && lngRaw !== "" ? parseFloat(lngRaw) : null;
    const driveId = driveMode ? (fd.get("drive_id") || null) : null;
    const commitTurn = driveMode ? reserveDriveCommit(driveId) : null;
    try {
    const capturedAtRaw = parseInt(fd.get("captured_at_ms"), 10);
    const sourceOffsetRaw = parseInt(fd.get("source_offset_ms"), 10);
    const gpsAccuracyRaw = parseFloat(fd.get("gps_accuracy"));
    const speedRaw = parseFloat(fd.get("speed"));
    const headingRaw = parseFloat(fd.get("heading"));
    const requestedSource = String(fd.get("capture_source") || "");
    const captureSource = driveMode
      ? (requestedSource === "drive_vod" ? "drive_vod" : "drive_live") : "manual";
    const sourceEventKey = driveMode && fd.get("source_event_key")
      ? String(fd.get("source_event_key")).slice(0, 180) : null;
    // Bind the request to the mode in which it began. A Settings change while a slow
    // request is finishing must not unpredictably change whether that observation saves.
    const dedupe = !S.debug;
    const normalizedHeading = Number.isFinite(headingRaw)
      ? ((headingRaw % 360) + 360) % 360 : null;
    const clientObservationId = sourceEventKey
      ? `capture-${await sha256HexText(sourceEventKey)}` : randomId();

    progress(driveMode ? pmsg("capture") : pmsg("compress"));
    // Measured on a real device: a 2000px frame is ~1.1 MB of base64 and every request
    // is marshalled across the JS-to-native bridge, which made a live detection call
    // take 13.5s median and stuttered the preview. The live pass therefore runs on a
    // smaller frame; the recorded footage keeps full quality, so a pothole missed live
    // is still recoverable by re-analysing the video. Single shots stay at full size:
    // one photo, someone waiting, and no footage behind it.
    // Both capture modes acquire and send exactly one full-frame image.
    let imageInputs, dataUrl;
    if (driveMode) {
      const driveInput = IMAGING_CONFIG.drive;
      dataUrl = await toDataUrl(photo, driveInput.maxDimension, driveInput.jpegQuality,
        driveInput.adaptiveBrightness, driveInput.roadBand);
      imageInputs = [{ url: dataUrl }];
    } else {
      const manualInput = IMAGING_CONFIG.manual;
      dataUrl = await toDataUrl(photo, manualInput.maxDimension, manualInput.jpegQuality,
        manualInput.adaptiveBrightness, manualInput.roadBand);
      imageInputs = [{ url: dataUrl }];
    }
    const shortOf = (g) => (g && g.short) || null;
    progress(pmsg("detect"));
    // Tender resolution can send exact coordinates to the project service and may use
    // shared model quota. It therefore begins only after the vision verdict accepts this
    // observation. Shared mode does not also contact public GIS/geocoder services from
    // the phone: the central resolver owns those checks and returns one authoritative
    // address, road-ownership and tender result.
    const detectPrompt = DETECT_PROMPT
      + (driveMode
        ? DETECTION_PROMPT_CONFIG.captureLayouts.drive
        : DETECTION_PROMPT_CONFIG.captureLayouts.manual)
      + (DETECTION_PROMPT_CONFIG.languageSuffixes[LANG()]
        || DETECTION_PROMPT_CONFIG.languageSuffixes.en);
    const detectionModel = S.model, detectionDetail = S.detail;
    // Single shot has one verdict on screen, so show it the moment it streams in.
    // Drive Mode analyses run concurrently and report through the HUD instead.
    // Drive Mode has no verdict on screen to update, so it passed no callback and took
    // the unstreamed path, waiting for a description it discards on every rejected frame.
    // It streams now purely to stop as soon as the frame is known to be rejected.
    const a = await analyzeImage(imageInputs, detectPrompt, "assessment", ASSESS_SCHEMA, detectionModel,
      driveMode ? null : emitVerdict, driveMode && !S.debug,
      detectionDetail, driveMode ? "drive" : "manual", `vision:${clientObservationId}`,
      { client_observation_id: clientObservationId, lat, lng });
    const decision = decisionFor(a);
    const accepted = decision === "accept";
    const detector = {
      model: detectionModel, detail: detectionDetail, prompt_version: PROMPT_VERSION,
      schema_version: SCHEMA_VERSION, evidence_count: imageInputs.length,
      provider: usingSharedVision() ? "shared_server" : "personal_openai",
      ...(a && a.detector && typeof a.detector === "object" ? a.detector : {}),
    };
    if (a && a.request_id) detector.request_id = a.request_id;
    const receiptText = a && typeof a.detection_receipt === "string"
      ? a.detection_receipt.trim().toLowerCase() : "";
    const detectionReceipt = /^[a-f0-9]{64}$/.test(receiptText) ? receiptText : null;
    if (accepted && usingSharedVision() && !detectionReceipt) {
      const error = new Error("The shared vision service returned no detection receipt. Try again.");
      error.sharedService = true;
      throw error;
    }
    if (driveMode && !accepted) {
      return { analyzed: true, accepted: false, stored: false, found: false,
               duplicate: false, duplicate_of: null, decision, review: decision === "review",
               ...a, observation: { ...a }, detector };
    }

    const duplicateResult = (existing) => driveMode
      ? { analyzed: true, accepted: true, stored: false, found: false,
          duplicate: true, duplicate_of: existing.id, existing_report_id: existing.id,
          skipped: "already reported nearby", decision, review: false,
          ...a, observation: { ...a }, detector }
      : { ...toDict(existing), duplicate: true, duplicate_of: existing.id };

    if (accepted) progress(pmsg("finalize"));
    // In personal mode the detector result is the critical path. Address, authority and
    // tender enrichment happen only when Email is tapped, so an unavailable GIS or
    // project server cannot make a completed vision verdict look frozen.
    const synchronousCentral = usingSharedVision();
    const deferEnrichment = accepted && !synchronousCentral
      && finiteCoord(lat) && finiteCoord(lng);
    const geo = accepted && !deferEnrichment && !usingSharedVision()
      ? await (lat != null ? reverseGeocode(lat, lng).catch(() => null) : Promise.resolve(null))
      : null;
    const localAddress = shortOf(geo);
    // Shared capture asks only the central resolver: it owns the authoritative KGIS
    // and geocoder checks. Repeating public Nominatim and four KGIS queries on the
    // phone could stall an accepted result for over a minute during an outage and
    // doubles upstream load at crowd scale. Personal mode retains its local path.
    let centralResolution = null;
    let tender = null;
    if (accepted && !deferEnrichment && finiteCoord(lat) && finiteCoord(lng)) {
      if (synchronousCentral) {
        // Keep this complete response local to the active observation. The tender,
        // road owner and shared-map LGD/town hints must all come from the same call.
        centralResolution = await tenderFromService(
          lat, lng, null, null, clientObservationId,
        ).catch((error) => ({ reached: false, tender: null, error }));
        tender = centralResolution.reached ? centralResolution.tender : null;
      } else {
        tender = await jurisdictionOf(lat, lng).catch(() => null)
          .then((w) => matchTender(localAddress, w && w.kind === "town" ? w.lgd : null,
            lat, lng, clientObservationId))
          .catch(() => null);
      }
    }
    const centralJurisdiction = centralResolution && centralResolution.reached
      ? centralResolution.jurisdiction : null;
    const address = localAddress || centralJurisdiction && centralJurisdiction.address || null;
    // A failed central lookup is still authoritative as "unknown" in shared mode.
    // Falling through to the phone's KGIS path here would duplicate public traffic,
    // bypass the server's bounded highway tolerance, and could disagree with the
    // ownership decision later made while the same observation is stored centrally.
    const routingJurisdiction = usingSharedVision()
      ? (centralJurisdiction || { road_ownership: "unknown" }) : null;
    const [officerName, officerEmail, unroutedReason, bodyName] = accepted && !deferEnrichment
      ? await routeOfficer((geo && geo.full) || address, lat, lng,
          routingJurisdiction) : [null, null, null, null];
    const covered = accepted && (deferEnrichment || !!officerEmail);
    if (accepted) progress(pmsg("write"));
    // No authority means no complaint. The photo, verdict and location are still kept,
    // so nothing is lost if coverage later extends to this place.
    const [subject, body] = accepted && covered
      ? draftEmail(a, lat, lng, address, officerName, tender)
      : [null, null];
    // Keep the full accepted evidence even when another device reported the same place.
    // Cross-device dedupe suppresses a second complaint, never the observer's evidence.
    const evidenceInput = IMAGING_CONFIG.acceptedEvidence;
    const photoFull = accepted
      ? (driveMode ? dataUrl : await toDataUrl(photo, evidenceInput.maxDimension,
          evidenceInput.jpegQuality, false)) : null;

    const rec = {
      created_at: Date.now() / 1000, lat, lng, address,
      client_observation_id: clientObservationId,
      photo: await dataUrlToBlob(dataUrl), photo_full: await dataUrlToBlob(photoFull),
      damage_type: a.damage_type, assessment: a.assessment, image_quality: a.image_quality,
      size: a.size,
      decision,
      description: a.description, email_subject: subject, email_body: body,
      status: accepted ? (covered ? "draft" : "unrouted") : (decision === "review" ? "review" : "rejected"),
      detection_model: detectionModel, image_detail: detectionDetail, prompt_version: PROMPT_VERSION,
      schema_version: SCHEMA_VERSION, evidence_count: imageInputs.length,
      // Distinguishes "we know where this is and do not cover it" from "we never got a
      // fix", which are the same status but very different things to tell someone.
      unrouted_reason: accepted && !covered ? (unroutedReason || "outside_area") : null,
      unrouted_body: accepted && !covered ? (bodyName || null) : null,
      officer_name: officerName, officer_email: officerEmail,
      tender_number: tender ? tender.tender_number : null,
      contractor: tender ? tender.contractor : null,
      tender_note: tender ? tender.note : null,
      tender_title: tender ? tender.title : null,
      tender_published: tender ? tender.published : null,
      tender_confidence: tender && Number.isFinite(tender.confidence) ? tender.confidence : null,
      tender_match_method: tender ? tender.match_method : null,
      tender_request_id: centralResolution && centralResolution.reached
        ? centralResolution.request_id : null,
      tender_resolution_reason: centralResolution && centralResolution.reached
        ? centralResolution.reason : null,
      tender_resolution_checked_at: centralResolution && centralResolution.reached
        ? Date.now() / 1000 : null,
      road_ownership: centralJurisdiction && centralJurisdiction.road_ownership || null,
      road_ownership_source: centralJurisdiction ? "central_v1" : null,
      body_lgd: centralJurisdiction && centralJurisdiction.road_ownership === "municipal"
        ? centralJurisdiction.lgd || null : null,
      body_name: centralJurisdiction && centralJurisdiction.road_ownership === "municipal"
        ? centralJurisdiction.town || null : bodyName || null,
      email_opened_at: null,
      sent_at: null,
      drive_id: driveId,
      capture_source: captureSource,
      source_event_key: sourceEventKey,
      source_event_keys: sourceEventKey ? [sourceEventKey] : [],
      captured_at: Number.isFinite(capturedAtRaw) ? capturedAtRaw / 1000 : null,
      source_offset_s: Number.isFinite(sourceOffsetRaw) ? sourceOffsetRaw / 1000 : null,
      gps_accuracy: Number.isFinite(gpsAccuracyRaw) ? gpsAccuracyRaw : null,
      speed_mps: Number.isFinite(speedRaw) ? speedRaw : null,
      heading: normalizedHeading,
      debug_capture: !dedupe,
      dedupe_eligible: accepted && dedupe,
      event_sightings: accepted ? [eventSighting({
        drive_id: driveId, lat, lng,
        source_offset_s: Number.isFinite(sourceOffsetRaw) ? sourceOffsetRaw / 1000 : null,
        captured_at: Number.isFinite(capturedAtRaw) ? capturedAtRaw / 1000 : null,
        gps_accuracy: Number.isFinite(gpsAccuracyRaw) ? gpsAccuracyRaw : null,
        speed_mps: Number.isFinite(speedRaw) ? speedRaw : null,
        heading: Number.isFinite(headingRaw) ? ((headingRaw % 360) + 360) % 360 : null,
        source_event_key: sourceEventKey,
      })] : [],
      sighting_drive_ids: accepted && driveId != null ? [String(driveId)] : [],
      seen_count: accepted ? 1 : 0,
      vision_provider: usingSharedVision() ? "shared_server" : "personal_openai",
      vision_request_id: a && a.request_id || null,
      last_seen_at: accepted
        ? (Number.isFinite(capturedAtRaw) ? capturedAtRaw / 1000 : Date.now() / 1000) : null,
    };
    if (accepted) {
      let centralReport = null;
      let centralRequestBody = null;
      let centralFailure = null;
      try {
        const centralRequest = await centralPotholeRequest(
          rec, dataUrl, detector, detectionReceipt, centralResolution);
        centralRequestBody = centralRequest ? JSON.stringify(centralRequest) : null;
        // Shared mode already proved the project service during preflight and needs its
        // cross-device duplicate answer now. Personal mode persists an outbox row and
        // returns the local result immediately; lifecycle retries deliver it later.
        const central = synchronousCentral
          ? (centralReport = await registerCentralPothole(centralRequest, centralRequestBody))
          : null;
        if (!synchronousCentral && centralRequestBody) {
          centralFailure = new Error("Shared-map sync queued until the project server is available.");
          rec.central_sync_pending = true;
          rec.server_sync_error = centralFailure.message;
        }
        if (central) {
          applyCentralPothole(rec, central);
          rec.server_duplicate = !!central.duplicate;
          rec.server_dedupe_distance_m = central.dedupe
            && Number.isFinite(central.dedupe.distance_m) ? central.dedupe.distance_m : null;
          if (central.duplicate) {
            rec.status = "duplicate";
            rec.duplicate = true;
            rec.duplicate_of = rec.server_pothole_id;
            // A central duplicate is impact evidence, but it must not create another
            // complaint for the same physical defect.
            rec.email_subject = null;
            rec.email_body = null;
          }
        }
      } catch (error) {
        // Detection and the local evidence stay useful during a service outage. Preserve
        // the request ID so support can locate the failed central attempt in logs. When
        // the exact observation body exists, commit it beside the report for retry.
        centralFailure = error;
        rec.central_sync_pending = !!centralRequestBody;
        rec.server_sync_error = error && error.message || "Shared-map sync failed.";
        rec.server_request_id = error && error.requestId || rec.server_request_id || null;
      }
      if (commitTurn) await commitTurn.wait;
      const centralOutboxRow = centralFailure && centralRequestBody ? {
        client_observation_id: rec.client_observation_id,
        path: "/v1/potholes/report",
        body: centralRequestBody,
        created_at: Date.now(),
        last_attempt_at: Date.now(),
        attempt_count: 1,
        last_error: rec.server_sync_error,
        last_request_id: rec.server_request_id || null,
      } : null;
      const committed = await addReportUnlessDuplicate(rec, dedupe, centralOutboxRow);
      if (committed.duplicate) {
        if (centralReport && committed.duplicate.server_pothole_id
            && String(committed.duplicate.server_pothole_id)
              === String(centralReport.pothole && centralReport.pothole.id)) {
          applyCentralPothole(committed.duplicate, centralReport);
          await putReport(committed.duplicate);
        }
        return duplicateResult(committed.duplicate);
      }
      rec.id = committed.id;
      if (!synchronousCentral) {
        // Delivery starts now when possible but never holds the detector result or
        // report screen. The shared probe is itself bounded and de-duplicated.
        void probeProjectService().then((available) => {
          if (available) return flushCentralOutbox();
        }).catch(() => {});
      }
    } else {
      rec.id = await addReport(rec);
    }
    return driveMode
      ? { analyzed: true, accepted: true, stored: true, found: !rec.server_duplicate,
          duplicate: !!rec.server_duplicate,
          duplicate_of: rec.server_duplicate ? rec.server_pothole_id : null,
          existing_report_id: rec.server_duplicate ? rec.server_pothole_id : null,
          decision, review: false,
          ...a, observation: { ...a }, detector, report: toDict(rec) }
      : toDict(rec);
    } finally {
      if (commitTurn) commitTurn.done();
    }
  }

  const CENTRAL_OWNERSHIP_SOURCE = "central_v1";
  const hasCentralOwnershipProof = (rec) => !!rec && !!rec.road_ownership
    && (rec.road_ownership_source === CENTRAL_OWNERSHIP_SOURCE || rec._native === true);
  const hasAuthoritativeMunicipalOwnership = (rec) => !!rec
    && rec.road_ownership === "municipal" && hasCentralOwnershipProof(rec);

  async function prepareComplaint(rec) {
    const lat = finiteCoord(rec && rec.lat) ? rec.lat : null;
    const lng = finiteCoord(rec && rec.lng) ? rec.lng : null;
    // The project service is the single ownership/tender authority for both detector
    // modes. Personal mode keeps images/key direct to OpenAI, but accepted coordinates
    // already go to this service and must not be routed by a conflicting phone-side rule.
    const centralAuthorityRequired = !!SERVICE_URL;
    let address = rec && rec.address || null;
    let jurisdiction = null;
    const ownershipReasons = new Set([
      "national_highway", "state_highway", "district_highway", "rural", "outside_state",
    ]);
    const persistedOwnership = rec && (rec.road_ownership
      || (ownershipReasons.has(rec.tender_resolution_reason)
        ? rec.tender_resolution_reason : null));
    let authoritativeJurisdiction = hasCentralOwnershipProof(rec) && persistedOwnership && {
        road_ownership: persistedOwnership,
        lgd: rec && rec.body_lgd || null,
        town: rec && rec.body_name || null,
        highway_name: rec && rec.unrouted_body || null,
        rural_body: rec && rec.unrouted_body || null,
      } || null;
    let ownershipSource = hasCentralOwnershipProof(rec) ? CENTRAL_OWNERSHIP_SOURCE : null;
    let tender = null;
    if (rec && rec.tender_number) {
      tender = {
        tender_number: rec.tender_number,
        contractor: rec.contractor || null,
        title: rec.tender_title || "",
        published: rec.tender_published || "",
        // Ignore legacy inferred DLP/maintenance values stored by older app builds.
        ...warrantyFor(rec.tender_published),
      };
    }

    // A shared observation is enriched by the central resolver only. This retry lets a
    // transient capture-time outage recover when Email is tapped without leaking a
    // parallel Nominatim/KGIS request from the phone or trusting stale local ownership.
    const sharedNeedsRevalidation = centralAuthorityRequired && rec
      && (rec.tender_resolution_checked_at == null || !hasCentralOwnershipProof(rec));
    if (sharedNeedsRevalidation && lat != null && lng != null) {
      // Use the central resolver based on the report's provider, not today's Settings.
      // A user may switch to a personal key after capture; that must not make a legacy
      // shared row trust cached municipal email/contractor text from an older build.
      const central = await tenderFromService(lat, lng, null, null,
        rec.client_observation_id || rec.source_event_key || `native-${rec.id}`)
        .catch(() => ({ reached: false, tender: null }));
      // Revalidation owns the answer. In particular, do not retain a legacy contractor
      // when the authoritative response says no tender or says this is a highway.
      tender = central && central.reached ? central.tender : null;
      authoritativeJurisdiction = central && central.reached && central.jurisdiction
        || { road_ownership: "unknown" };
      ownershipSource = central && central.reached ? CENTRAL_OWNERSHIP_SOURCE : null;
    } else if (centralAuthorityRequired && !authoritativeJurisdiction) {
      authoritativeJurisdiction = { road_ownership: "unknown" };
    }
    const authoritativeMunicipal = authoritativeJurisdiction
      && authoritativeJurisdiction.road_ownership === "municipal";
    if (centralAuthorityRequired) {
      address = address || authoritativeJurisdiction.address || null;
    }
    if (lat != null && lng != null) {
      if (!centralAuthorityRequired) {
        const [geo, where] = await Promise.all([
          address ? Promise.resolve(null) : reverseGeocode(lat, lng).catch(() => null),
          authoritativeJurisdiction
            ? Promise.resolve(null) : jurisdictionOf(lat, lng).catch(() => null),
        ]);
        address = address || geo && geo.short || null;
        jurisdiction = where;
      }
    }
    let officerName = rec && (rec.officer_name || rec.officer_title) || null;
    let officerEmail = rec && (rec.officer_email || rec.email_to) || null;
    // A server ownership result outranks any recipient cached by an older build.
    // Re-resolve even municipal rows so a highway answer can never inherit a stale
    // city Commissioner and become sendable.
    if (authoritativeJurisdiction) {
      officerName = null;
      officerEmail = null;
    }
    let unroutedReason = null, unroutedBody = null;
    if (!officerEmail) {
      [officerName, officerEmail, unroutedReason, unroutedBody] =
        await routeOfficer(address, lat, lng, authoritativeJurisdiction);
    }
    if (!officerEmail) {
      throw complaintRouteError(unroutedReason || "road_class_unknown", unroutedBody, {
        address,
        bodyLgd: authoritativeJurisdiction
          ? authoritativeMunicipal && authoritativeJurisdiction.lgd || null
          : rec && rec.body_lgd || jurisdiction && jurisdiction.lgd || null,
        bodyName: authoritativeJurisdiction
          ? authoritativeMunicipal && authoritativeJurisdiction.town || null
          : rec && rec.body_name || jurisdiction && jurisdiction.name || null,
      });
    }

    if (!centralAuthorityRequired && !tender && rec && rec.tender_resolution_checked_at == null
        && lat != null && lng != null) {
      const lgd = authoritativeJurisdiction
        ? authoritativeMunicipal && authoritativeJurisdiction.lgd || null
        : rec.body_lgd || jurisdiction && jurisdiction.kind === "town" && jurisdiction.lgd || null;
      tender = await matchTender(address, lgd, lat, lng,
        rec.client_observation_id || rec.source_event_key || `native-${rec.id}`).catch(() => null);
    }
    const [subject, body] = draftEmail(
      rec || {}, lat, lng, address, officerName, tender);
    const roadOwnership = authoritativeJurisdiction
      && authoritativeJurisdiction.road_ownership || null;
    return { to: officerEmail, officer_name: officerName, subject, body,
      address, road_ownership: roadOwnership, road_ownership_source: ownershipSource,
      body_lgd: authoritativeJurisdiction
        ? authoritativeMunicipal && authoritativeJurisdiction.lgd || null
        : rec.body_lgd || jurisdiction && jurisdiction.lgd || null,
      body_name: authoritativeJurisdiction
        ? authoritativeMunicipal && authoritativeJurisdiction.town || null
        : rec.body_name || jurisdiction && jurisdiction.name || null,
      tender, tender_number: tender && tender.tender_number || null };
  }

  async function openEmailDraft(rec) {
    // Always the routed officer. The app never sends; the user does, in their email app.
    // No fallback recipient: an unrouted report must not borrow Bengaluru's address.
    if (!centralReportIsConfirmed(rec)) {
      const error = new Error(
        "The shared-map duplicate check must finish before this email can be opened.",
      );
      error.code = "central_sync_pending";
      error.report = toDict(rec);
      throw error;
    }
    let prepared;
    try {
      prepared = hasAuthoritativeMunicipalOwnership(rec)
          && rec.officer_email && rec.email_subject && rec.email_body
          && rec.tender_resolution_checked_at != null
        ? { to: rec.officer_email, officer_name: rec.officer_name || null,
            subject: rec.email_subject, body: rec.email_body,
            address: rec.address || null, body_lgd: rec.body_lgd || null,
            body_name: rec.body_name || null,
            road_ownership: rec.road_ownership,
            road_ownership_source: rec.road_ownership_source,
            tender: rec.tender_number ? {
              tender_number: rec.tender_number, contractor: rec.contractor || null,
              note: rec.tender_note || null, title: rec.tender_title || null,
              published: rec.tender_published || null,
              confidence: rec.tender_confidence,
              match_method: rec.tender_match_method || null,
            } : null }
        : await prepareComplaint(rec);
    } catch (error) {
      if (error && error.code === "complaint_unrouted") {
        // Personal-key detection returns before the slower GIS lookup. If that lookup
        // cannot name an authority, convert the optimistic local draft into the same
        // durable unrouted state produced by synchronous/shared capture. A later history
        // render must not keep offering an Email button that can never have a recipient.
        rec.status = "unrouted";
        rec.unrouted_reason = error.unroutedReason || "road_class_unknown";
        rec.unrouted_body = error.unroutedBody || null;
        rec.address = error.address || rec.address || null;
        rec.body_lgd = error.bodyLgd || rec.body_lgd || null;
        rec.body_name = error.bodyName || rec.body_name || null;
        rec.officer_name = null;
        rec.officer_email = null;
        rec.email_subject = null;
        rec.email_body = null;
        await putReport(rec);
        error.report = toDict(rec);
      }
      throw error;
    }
    rec.officer_email = prepared.to;
    rec.officer_name = prepared.officer_name || rec.officer_name || null;
    rec.address = prepared.address || rec.address || null;
    rec.body_lgd = prepared.body_lgd || rec.body_lgd || null;
    rec.body_name = prepared.body_name || rec.body_name || null;
    rec.road_ownership = prepared.road_ownership || rec.road_ownership || null;
    rec.road_ownership_source = prepared.road_ownership_source
      || rec.road_ownership_source || null;
    rec.email_subject = prepared.subject;
    rec.email_body = prepared.body;
    rec.tender_resolution_checked_at = rec.tender_resolution_checked_at || Date.now() / 1000;
    if (prepared.tender) {
      rec.tender_number = prepared.tender.tender_number || null;
      rec.contractor = prepared.tender.contractor || null;
      rec.tender_note = prepared.tender.note || null;
      rec.tender_title = prepared.tender.title || null;
      rec.tender_published = prepared.tender.published || null;
      rec.tender_confidence = Number.isFinite(prepared.tender.confidence)
        ? prepared.tender.confidence : null;
      rec.tender_match_method = prepared.tender.match_method || null;
    } else {
      rec.tender_number = null;
      rec.contractor = null;
      rec.tender_note = null;
      rec.tender_title = null;
      rec.tender_published = null;
      rec.tender_confidence = null;
      rec.tender_match_method = null;
    }
    progress(pmsg("email"));
    if (NATIVE) {
      // Vanilla-JS WebView: the injected runtime exposes plugins via Capacitor.Plugins
      // and has no registerPlugin. Support both for bundler compatibility.
      const EmailComposer = Capacitor.registerPlugin
        ? Capacitor.registerPlugin("EmailComposer")
        : Capacitor.Plugins.EmailComposer;
      await EmailComposer.open({
        to: [prepared.to],
        subject: prepared.subject,
        body: prepared.body,
        // Full capture where we kept one; the working copy is only a fallback.
        attachments: [{ type: "base64", name: "road-damage.jpg",
                        path: await photoToBase64(rec.photo_full || rec.photo) }],
      });
    } else {
      console.log("[harness] would open native compose to:", prepared.to);
    }
    rec.status = "queued";
    rec.email_opened_at = rec.email_opened_at || rec.sent_at || Date.now() / 1000;
    rec.sent_at = null;
    await putReport(rec);
    return toDict(rec);
  }

  // ---------- dataset export ----------
  // A stored-entry ZIP, written by hand: JPEGs are already compressed, so there is
  // nothing to gain from deflate and no reason to pull in a zip library.
  const CRC = (() => {
    const t = new Uint32Array(256);
    for (let n = 0; n < 256; n++) {
      let c = n;
      for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
      t[n] = c >>> 0;
    }
    return (buf) => {
      let c = 0xffffffff;
      for (let i = 0; i < buf.length; i++) c = t[(c ^ buf[i]) & 0xff] ^ (c >>> 8);
      return (c ^ 0xffffffff) >>> 0;
    };
  })();

  function zip(files) {
    const enc = new TextEncoder();
    const chunks = [], central = [];
    let offset = 0;
    const u16 = (n) => [n & 255, (n >> 8) & 255];
    const u32 = (n) => [n & 255, (n >> 8) & 255, (n >> 16) & 255, (n >>> 24) & 255];
    for (const f of files) {
      const name = enc.encode(f.name);
      const crc = CRC(f.data);
      const local = new Uint8Array([...u32(0x04034b50), ...u16(20), ...u16(0), ...u16(0),
        ...u16(0), ...u16(0), ...u32(crc), ...u32(f.data.length), ...u32(f.data.length),
        ...u16(name.length), ...u16(0)]);
      chunks.push(local, name, f.data);
      central.push(new Uint8Array([...u32(0x02014b50), ...u16(20), ...u16(20), ...u16(0),
        ...u16(0), ...u16(0), ...u16(0), ...u32(crc), ...u32(f.data.length), ...u32(f.data.length),
        ...u16(name.length), ...u16(0), ...u16(0), ...u16(0), ...u16(0), ...u32(0),
        ...u32(offset)]), name);
      offset += local.length + name.length + f.data.length;
    }
    const centralSize = central.reduce((n, c) => n + c.length, 0);
    const end = new Uint8Array([...u32(0x06054b50), ...u16(0), ...u16(0),
      ...u16(files.length), ...u16(files.length), ...u32(centralSize), ...u32(offset), ...u16(0)]);
    const all = [...chunks, ...central, end];
    const out = new Uint8Array(all.reduce((n, c) => n + c.length, 0));
    let at = 0;
    for (const c of all) { out.set(c, at); at += c.length; }
    return out;
  }

  const b64ToBytes = (b64) => {
    const bin = atob(b64);
    const out = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
    return out;
  };
  const bytesToB64 = (bytes) => {
    let s = "";
    for (let i = 0; i < bytes.length; i += 0x8000) {
      s += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
    }
    return btoa(s);
  };

  // Exports only what a human actually labelled: a model verdict is not ground truth,
  // and a benchmark built from the detector's own opinions cannot measure the detector.
  async function exportDataset() {
    const labelled = (await allReports()).filter((r) => r.human_label);
    if (!labelled.length) throw new Error("Nothing labelled yet. Open Review frames and tag some first.");
    const files = [], index = [];
    for (const r of labelled) {
      const name = `images/frame-${r.id}.jpg`;
      files.push({ name, data: b64ToBytes(await photoToBase64(r.photo)) });
      index.push({
        path: name,
        label: r.human_label,
        labelled_by: "owner",
        model_said: {
          damage_type: damageTypeOf(r), assessment: assessmentOf(r),
          image_quality: r.image_quality || null,
          decision: r.decision || (r.status === "rejected" ? "reject" : "accept"),
          size: r.size, description: r.description,
        },
        detector: { model: r.detection_model || "legacy", detail: r.image_detail || null,
                    prompt_version: r.prompt_version || "legacy", schema_version: r.schema_version || 1,
                    evidence_count: r.evidence_count || 1 },
        lat: r.lat, lng: r.lng, address: r.address,
        server_pothole_id: r.server_pothole_id || null,
        drive_id: r.drive_id, captured_at: new Date(r.created_at * 1000).toISOString(),
      });
    }
    files.push({ name: "labels.json", data: new TextEncoder().encode(
      JSON.stringify({ exported_at: new Date().toISOString(), count: index.length, images: index }, null, 1)) });
    const bytes = zip(files);
    return { name: `road-damage-dataset-${Date.now()}.zip`, base64: bytesToB64(bytes),
             count: index.length, bytes: bytes.length };
  }

  // ---------- API dispatch ----------
  async function handle(path, opts) {
    const method = ((opts && opts.method) || "GET").toUpperCase();
    let m;
    if (path === "/api/health") {
      const base = {
        provider: usingSharedVision() ? "shared_server" : "personal_openai",
        service_url: SERVICE_URL, delivery: "email_compose", email_configured: true,
        detection_model: S.model, image_detail: S.detail, prompt_version: PROMPT_VERSION,
      };
      if (!usingSharedVision()) return { ...base, ai_configured: !!S.key };
      try {
        const remote = await serviceGet("/v1/health", 4000);
        projectServiceState = remote && remote.ok ? "available" : "unavailable";
        projectServiceCheckedAt = Date.now();
        return {
          ...base,
          ai_configured: !!(remote && remote.ok && remote.shared_vision_configured === true),
          shared_backend_provider: remote && remote.shared_vision_provider || null,
          detection_model: remote && remote.shared_vision_provider === "http_yolo"
            ? remote.shared_vision_model || base.detection_model : base.detection_model,
          service_error: remote && remote.shared_vision_configured === false
            ? "The shared vision detector is not configured." : null,
          service_request_id: remote && remote.request_id || null,
        };
      } catch (error) {
        projectServiceState = "unavailable";
        projectServiceCheckedAt = Date.now();
        return {
          ...base,
          ai_configured: false,
          service_error: error && error.message || "The shared vision service is unavailable.",
          service_request_id: error && error.requestId || null,
        };
      }
    }
    if (path === "/api/map" && method === "GET") return serviceGet("/v1/map");
    if (path.startsWith("/api/impact") && method === "GET") {
      const suffix = path.slice("/api/impact".length);
      return serviceGet(`/v1/impact${suffix}`);
    }
    if (path === "/api/reports" && method === "GET") {
      // Without photo_full. The evidence copy is a 4000px JPEG and the list only shows a
      // thumbnail, so shipping it here cost about a megabyte per report on every return
      // to the home screen, and the cost grew with every pothole ever reported. The only
      // reader of it is the email attachment, which loads the record by id anyway.
      return (await allReports()).sort((a, b) => b.id - a.id).map(listDict);
    }
    if (path === "/api/reports" && method === "DELETE") {
      await op("readwrite", (s) => s.clear());
      await op("readwrite", (s) => s.clear(), "central_outbox");
      await op("readwrite", (s) => s.clear(), "drives");
      await op("readwrite", (s) => s.clear(), "footage");
      await op("readwrite", (s) => s.clear(), "identity");
      installationCache = null;
      return { ok: true };
    }
    if (path === "/api/drives" && method === "GET") return allDrives();
    if (path === "/api/footage" && method === "POST") {
      const fd = opts.body;
      const blob = fd.get("segment"), driveId = String(fd.get("drive_id"));
      const seq = parseInt(fd.get("seq"), 10) || 0;
      const recordingStartedRaw = parseInt(fd.get("recording_started_at_ms"), 10);
      const sourceOffsetRaw = parseInt(fd.get("source_offset_ms"), 10);
      if (!blob || !blob.size) throw new Error("Empty footage segment.");
      await putFootage({ key: `${driveId}#${String(seq).padStart(5, "0")}`, drive_id: driveId,
                         seq, blob, mime: blob.type || "video/mp4", bytes: blob.size,
                         recording_started_at_ms: Number.isFinite(recordingStartedRaw) ? recordingStartedRaw : null,
                         source_offset_s: Number.isFinite(sourceOffsetRaw) ? sourceOffsetRaw / 1000 : null,
                         at: Date.now() / 1000 });
      return { ok: true, bytes: blob.size };
    }
    // Summaries only: the caller asks for the blobs separately, because a drive's
    // footage is hundreds of megabytes and must never be materialised by accident.
    if (path === "/api/footage" && method === "GET") {
      const byDrive = {};
      for (const f of await footageMetadata()) {
        const clipStart = Number.isFinite(f.recording_started_at_ms)
          ? f.recording_started_at_ms / 1000 : f.at;
        const d = byDrive[f.drive_id] || (byDrive[f.drive_id] = {
          drive_id: f.drive_id, segments: 0, bytes: 0, mime: f.mime,
          started_at: clipStart || null, ended_at: f.at || clipStart || null,
        });
        d.segments++; d.bytes += f.bytes;
        if (clipStart) {
          d.started_at = d.started_at == null ? clipStart : Math.min(d.started_at, clipStart);
        }
        if (f.at || clipStart) {
          const clipEnd = f.at || clipStart;
          d.ended_at = d.ended_at == null ? clipEnd : Math.max(d.ended_at, clipEnd);
        }
      }
      return Object.values(byDrive);
    }
    if ((m = path.match(/^\/api\/footage\/([^/]+)\/manifest$/)) && method === "GET") {
      const segs = (await footageMetadata(decodeURIComponent(m[1])))
        .sort((a, b) => a.seq - b.seq);
      if (!segs.length) throw new Error("No footage stored for that drive.");
      return { mime: segs[0].mime, clips: segs };
    }
    if ((m = path.match(/^\/api\/footage\/([^/]+)\/clip\/([^/]+)$/)) && method === "GET") {
      const driveId = decodeURIComponent(m[1]);
      const clip = await getFootage(decodeURIComponent(m[2]));
      if (!clip || String(clip.drive_id) !== driveId || !clip.blob) {
        throw new Error("Footage segment not found.");
      }
      // The analyser asks for exactly one segment at a time. Never return neighbouring
      // Blob values here: their metadata already came from the lightweight manifest.
      return {
        key: String(clip.key), drive_id: String(clip.drive_id),
        seq: Number.isFinite(clip.seq) ? clip.seq : 0,
        mime: clip.mime || clip.blob.type || "video/mp4",
        bytes: Number.isFinite(clip.bytes) ? clip.bytes : clip.blob.size,
        recording_started_at_ms: Number.isFinite(clip.recording_started_at_ms)
          ? clip.recording_started_at_ms : null,
        source_offset_s: Number.isFinite(clip.source_offset_s) ? clip.source_offset_s : null,
        at: Number.isFinite(clip.at) ? clip.at : null,
        blob: clip.blob,
      };
    }
    if ((m = path.match(/^\/api\/footage\/([^/]+)\/blobs$/)) && method === "GET") {
      const segs = (await footageFor(decodeURIComponent(m[1]))).sort((a, b) => a.seq - b.seq);
      if (!segs.length) throw new Error("No footage stored for that drive.");
      return {
        mime: segs[0].mime,
        // `blobs` keeps the old API shape for callers/tests. `clips` carries the true
        // recorder timeline, including gaps and failed sequence numbers.
        blobs: segs.map((x) => x.blob),
        clips: segs.map((x) => ({ seq: x.seq, blob: x.blob,
          recording_started_at_ms: x.recording_started_at_ms || null,
          source_offset_s: Number.isFinite(x.source_offset_s) ? x.source_offset_s : null })),
      };
    }
    if ((m = path.match(/^\/api\/footage\/([^/]+)$/)) && method === "DELETE") {
      const id = decodeURIComponent(m[1]);
      await deleteFootageFor(id);
      return { ok: true };
    }
    if (path === "/api/drives" && method === "POST") {
      const d = JSON.parse(opts.body);
      if (!d || !d.id) throw new Error("Drive id missing.");
      const alreadyIds = Array.isArray(d.already_ids)
        ? [...new Set(d.already_ids.map((x) => String(x).slice(0, 64)))] : [];
      await putDrive({ id: String(d.id), started_at: d.started_at || null,
                       ended_at: Date.now() / 1000, checked: d.checked | 0, found: d.found | 0,
                       already: Math.max(d.already | 0, alreadyIds.length), already_ids: alreadyIds,
                       gps_track: Array.isArray(d.gps_track) ? d.gps_track : [] });
      return { ok: true };
    }
    if ((m = path.match(/^\/api\/drives\/([^/]+)\/analysis$/)) && method === "POST") {
      const id = decodeURIComponent(m[1]);
      const stats = JSON.parse(opts.body || "{}");
      const prior = await getDrive(id) || {
        id, started_at: stats.started_at || null, ended_at: Date.now() / 1000,
        checked: 0, found: 0, already: 0, already_ids: [], gps_track: [],
      };
      const priorIds = Array.isArray(prior.already_ids) ? prior.already_ids : [];
      const incomingIds = Array.isArray(stats.already_ids) ? stats.already_ids : [];
      prior.already_ids = [...new Set([...priorIds, ...incomingIds]
        .map((x) => String(x).slice(0, 64)))];
      prior.already = Math.max(prior.already | 0, prior.already_ids.length);
      prior.analysis_checked = Math.max(0, stats.checked | 0);
      prior.analysis_found = Math.max(0, stats.found | 0);
      prior.analysis_already = Math.max(0, stats.already | 0, incomingIds.length);
      prior.analysis_at = Date.now() / 1000;
      await putDrive(prior);
      return { ok: true };
    }
    if (path === "/api/export" && method === "POST") return exportDataset();
    if ((m = path.match(/^\/api\/reports\/(\d+)\/label$/)) && method === "POST") {
      const rec = await getReport(m[1]);
      if (!rec) throw new Error("Report not found.");
      const want = JSON.parse(opts.body).label;
      if (!["pothole_cavity", "failed_patch", "surface_breakup", "rut_or_depression",
            "other_road_damage", "undamaged", "pothole", "not_pothole", null].includes(want)) {
        throw new Error("Bad label.");
      }
      rec.human_label = want;
      await putReport(rec);
      return toDict(rec);
    }
    if (path === "/api/report" && method === "POST") return createReport(opts.body, false);
    if (path === "/api/frame" && method === "POST") return createReport(opts.body, true);
    if ((m = path.match(/^\/api\/reports\/(\d+)\/send$/)) && method === "POST") {
      const rec = await getReport(m[1]);
      if (!rec) throw new Error("Report not found.");
      if (rec.status === "duplicate" || rec.server_duplicate) {
        throw new Error("This pothole was already reported nearby, so a duplicate complaint was not created.");
      }
      if (rec.status === "unrouted") {
        // Say which of the four reasons it was. "Outside the area" is wrong and
        // confusing when the real problem is that the phone never got a GPS fix.
        throw complaintRouteError(rec.unrouted_reason, rec.unrouted_body, {
          report: toDict(rec),
        });
      }
      if (!centralReportIsConfirmed(rec)) {
        throw new Error("The shared-map duplicate check must finish before this email can be opened.");
      }
      // "queued" stays reopenable: canceling the email composer must not strand the report.
      if (rec.status === "sent") rec.status = "queued";
      if (rec.status !== "draft" && rec.status !== "queued") throw new Error("This report is not a sendable draft.");
      return openEmailDraft(rec);
    }
    if ((m = path.match(/^\/api\/reports\/(\d+)$/))) {
      const rec = await getReport(m[1]);
      if (!rec) throw new Error("Report not found.");
      if (method === "PATCH") {
        if (rec.status !== "draft" && rec.status !== "queued") throw new Error("Only drafts can be edited.");
        const upd = JSON.parse(opts.body);
        rec.email_subject = upd.email_subject;
        rec.email_body = upd.email_body;
        await putReport(rec);
        return toDict(rec);
      }
      if (method === "DELETE") {
        await deleteReportAndCentralOutbox(rec.id);
        return { ok: true };
      }
    }
    throw new Error(`Unhandled: ${method} ${path}`);
  }

  // Native hardware back button routes through window.handleAppBack (defined by the UI).
  if (NATIVE) {
    try {
      const App = Capacitor.Plugins.App;
      if (App && App.addListener) {
        App.addListener("backButton", () => {
          if (!(window.handleAppBack && window.handleAppBack())) App.exitApp();
        });
      }
    } catch (e) {}
  }

  // Pure helpers, exposed for tests. These are references, not copies: a test exercises
  // exactly the code that runs in production. Nothing here holds state or a secret.
  const __pure = { inCoverage, peekVerdict, peekReject, rejectedVerdict, decisionFor,
                   damageTypeOf, assessmentOf, normaliseModel, normaliseDetail,
                   buildDetectionRequest, effectiveVisionProvider,
                   ASSESS_SCHEMA, DETECT_PROMPT, PROMPT_VERSION,
                   buildTenderMatchRequest,
                   SCHEMA_VERSION, MAX_DETECTION_IMAGES, ROAD_BAND, averageLuminance,
                   distMeters, roadEventMatch, sameRoadEvent, findDuplicateReport,
                   draftEmail, dataUrlToBlob, photoToBase64, toDict, listDict,
                   warrantyFor, shortlistFor, matchTenderFor: matchTender,
                   centralReportIsConfirmed,
                   canonicalServiceRequest };

  window.StandaloneAPI = { __pure, handle, prewarm, prepareComplaint };

  // Pending accepted observations contain no image or complaint text. Retry them only
  // at bounded lifecycle signals; the stable body/idempotency key prevents double count.
  window.addEventListener("online", () => {
    if (projectServiceAvailable()) {
      void flushCentralOutbox().catch(() => {});
      return;
    }
    projectServiceState = "unknown";
    projectServiceCheckedAt = 0;
    void probeProjectService().then((available) => {
      if (available) return flushCentralOutbox();
    }).catch(() => {});
  });

  // Warm the shared service and flush pending accepted observations on startup. A fresh
  // install needs no setup screen: without a personal key its effective provider is the
  // shared detector automatically.
  window.addEventListener("load", () => {
    void probeProjectService().then((available) => {
      if (available) return flushCentralOutbox();
    }).catch(() => {});
  });
})();
