# Government complaint boundary

Status: email is the only complaint-filing path. Last evidence review: 5 September 2026.

## Current behaviour

Pothole Reporter does not create a grievance through a government API and does not
automatically transmit a complaint to GBA, BBMP, Sahaaya, or any other authority.
The central service is limited to detection support, geolocation and jurisdiction
resolution, probable tender matching, deduplication, the public aggregate map,
and impact metrics.

After the user reviews a detected pothole, the app opens one prefilled email draft
addressed to the commissioner or responsible authority selected by its jurisdiction
registry. The draft includes the location, map link, pothole details, and evidence
photo. It includes a probable tender number only when a tender was actually found.
Footpath-, drain-, utility-, and other non-road-only works are excluded from that
match; an exact street or ward name alone is insufficient.
The user's email app remains responsible for attachment handling and sending; Pothole
Reporter never presses Send.

## Why there is no automatic GBA connector

The [official GBA portal](https://gba.karnataka.gov.in/) lists Sahaaya 2.0 as a
public-grievance service. Official BBMP material describes photo/video and geotagged
pothole workflows:

- [BBMP administrative-reforms report](https://bbmp.gov.in/ucc_file/KarnatakaAdministrativeReforms.pdf)
- [2023–24 BBMP budget speech](https://bbmp.gov.in/ucc_file/FINAL%20BUDGET%20SPEECH%20BBMP%2002-03-2023.pdf)

Those public sources do not publish a machine-to-machine complaint-creation API,
authentication method, request schema, sandbox, status callback, or service-level
agreement. This project therefore does not scrape a citizen form, call an observed
private endpoint, queue government-delivery webhooks, or claim that creating a local
map record creates an official complaint.

If an official integration is considered in the future, it requires a separate
product decision and explicit approval covering endpoint ownership, authentication,
evidence consent, retention, routing, acknowledgements, and status synchronization.
It must not silently replace or supplement the user-controlled email flow.
