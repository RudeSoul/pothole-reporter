# Where the app's data and judgments come from

This document records provenance, transformations, and limits. The app combines a
user's camera and phone location with government reference data, OpenStreetMap data,
local matching rules, and AI-generated judgments. A source record is not proof that a
particular defect, authority, or contract match is correct. The user must review the
photo, routing, and any probable contract match before sending a complaint.

## Road contracts (42,283 rows)

**Source: KPPP, the Karnataka Public Procurement Portal, Government of Karnataka.**
<https://kppp.karnataka.gov.in>

This is the state's own e-procurement portal. The app uses the same awarded-tender
search that the portal's public website uses, which needs no login, no API key and no
registration. Reproduce it:

```bash
curl -s -X POST "https://kppp.karnataka.gov.in/supplier-registration-service/v1/api/portal-service/works/search-eproc-tenders?page=0&size=1000&order-by-tender-publish=true" \
  -H "content-type: application/json" \
  -H "Referer: https://kppp.karnataka.gov.in/" \
  -H "Post: CONTRACTOR-EPROC-CONTRACTOR" \
  -d '{"category":"WORKS","status":"AWARDED"}' -D - -o page0.json | grep -i x-total-count
```

That header reports the total the portal holds: **98,009 awarded works** at the time of
writing. `tools/pull-kppp.py` walks those pages and keeps the road-related ones by
matching the work title, which is why the bundle is 42,283 rows rather than all 98,009.

**Verification done on 19 Aug 2026:** of the 1,000 rows on the portal's first page,
**341 appear in the bundled file, byte-identical on title, publication date and
location**. The rest of that page is non-road work the sweep deliberately drops.

Fields taken: `tenderNumber`, `description` (the work title), `locationName`,
`publishedDate`. Nothing is edited beyond truncating long titles.

## Contractor names (1,124 of those rows)

**Source: the public-domain snapshot at
<https://bengaluru-road-contracts.pages.dev>, whose own source is KPPP.**

The portal's *search* results do not include the winning bidder; only the per-tender
full view does. So contractor names come from that pre-existing snapshot of Bengaluru
contracts. Outside Bengaluru the complaint names the tender and states plainly that no
winning bidder is recorded. It never guesses a company name.

## Which officer receives the complaint (182 local bodies)

**Sources: the district NIC sites of the Government of Karnataka, and the bodies' own
sites on the Municipal Reforms Cell domain.**
Pattern: `https://<district>.nic.in/en/public-utility-category/municipality/` and
`https://<name>city.mrc.gov.in`.

Every entry in [`data/karnataka-bodies.json`](../data/karnataka-bodies.json) carries a
`source` field naming the page its address was read from. The 18 city corporations were
each checked twice, by separate passes, because they carry the most traffic. A body whose
address could not be found on a government page is **not** in the file, and the app
refuses to name a recipient for it rather than guessing.

## Which body owns the road

**Source: KGIS, run by KSRSAC, the Karnataka State Remote Sensing Applications Centre.**

The central service asks the state's own GIS which road class and local-body boundary
cover the pothole. It first queries the KGIS National Highway, State Highway, and
District Highway layers (MapServer layers 289, 290, and 291) within a bounded GPS
tolerance. A hit is terminal: the app does not name a municipal officer, tender, or
contractor for that location. Only after all three highway checks succeed with no hit
does it use the Town boundary layer:

```bash
curl -s "https://kgis.ksrsac.in/kgismaps/rest/services/Boundaries/Admin_Dynamic_New/MapServer/1/query?geometry=76.6394,12.2958&geometryType=esriGeometryPoint&inSR=4326&spatialRel=esriSpatialRelIntersects&outFields=KGISTownName,Town_Type,LGD_TownCode&returnGeometry=false&f=json"
```

Returns `MYSURU`, type `CC`, LGD code `252045`. That layer holds exactly 319 polygons,
matching the state's 319 urban local bodies. A point outside a Town polygon falls
through to the gram-panchayat layer and is not routed to a municipal recipient. If any
required KGIS check is unavailable or malformed, ownership stays unknown and routing
fails closed.

The officer directory is keyed on **LGD_TownCode**, the Local Government Directory code
issued by the Ministry of Panchayati Raj, so a body is identified by a national
identifier rather than by matching a place name.

## Street address

**Source: OpenStreetMap, via Nominatim.** Used only to turn coordinates into a street
name and pincode for the complaint. The coordinates themselves come from the phone's GPS
and are printed in the complaint alongside a map link, so the location can be checked
independently of any geocoder.

## What is generated or inferred

The road-damage verdict is produced by an AI vision model looking at exactly one
photograph. Its five fields are image quality (`acceptable` or `rejected`), assessment
(`damaged` or `undamaged`), nullable damage type, nullable size, and a one-line
description. Asphalt, concrete, gravel, dirt, and mud roads are in scope. Road-edge
damage is still damage when it touches a kerb, gutter, shoulder, drain surround, or
road-to-footpath joint; damage confined to an intact non-road object is not. The result
is a judgement about a photograph, not a record, which is why the photograph is always
attached: the officer can disagree by looking.

A probable contract match is also a judgment, not a procurement record. The app first
builds a deterministic local shortlist from address words and contracts indexed to the
same local body, then asks an AI model whether one candidate clearly covers that road or
locality. A confidence threshold can reject weak matches, but it cannot turn an accepted
match into proof. The complaint asks the receiving officer to verify it against the
tender documents.

The adjudication policy excludes footpath-, sidewalk-, pedestrian-walkway-, kerb-,
drain-, culvert-, utility-, landscaping-, building-, park-, and other non-road-only
works even when they name the exact street, locality, or ward. A combined work is
eligible only when its text explicitly also covers road-surface repair, resurfacing,
pavement, pothole filling, rehabilitation, or road maintenance. Prompt/threshold
changes are checked with `python3 eval/run_tender_eval.py`, which reports precision and
recall on sealed human-labelled positive and negative cases; an all-null matcher cannot
pass that gate.

The app does **not infer** defect-liability or maintenance status from a tender's
publication date. These records do not provide the award, completion or contractual
liability dates needed to establish a current contractor obligation. A complaint may
include a probable tender and recorded winning bidder, but explicitly asks the officer
to verify the tender, award, work order and applicable terms before assigning liability.

## Known limits

- 137 of Karnataka's 319 local bodies have no address in the file, because their district
  pages publish none. Those reports refuse to route.
- 41,159 of the 42,283 contracts have no winning bidder recorded, for the reason above.
- Of the 42,283 bundled rows, 18,972 are municipal (DMA or legacy BBMP) records. Only
  13,577 are currently indexed to a supported body and can enter the app's contract
  shortlist. The remaining municipal rows are unresolved or belong to bodies without a
  published address; the 23,311 non-municipal rows belong to agencies such as PWD,
  panchayats, and irrigation departments and are not candidates for a municipal complaint.
- The initial title-keyword filter is intentionally inclusive. The deterministic
  shortlist and adjudicator's explicit non-road-only exclusion both matter; measured
  tender precision and recall, rather than null-rate alone, gate prompt changes.
- Contracts are a snapshot. Re-run `tools/pull-kppp.py` to refresh.
