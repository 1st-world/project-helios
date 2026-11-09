Language: ***English*** | [한국어](README.ko-kr.md)

# Shared Provider records

Sanitized requests, responses, and reported usage from Helios Provider calls. Recorded tests use synthetic inputs. Earlier saved conversation observations are labeled separately.

- [Record catalog](results.json) lists the available runs and observations.
- [M01 memory follow-up](runs/2026-11-09/M01-01.json) contains the DeepSeek V4.1 Flash summary and recall exchanges.
- [October 31 call summary](runs/2026-10-31/summary.json) and [older captures](history/api-validation-results.json) provide earlier evidence.

## Reading the records

- `exchanges` contains the published HTTP payloads and stream events. `calls` contains the app ledger. `result` explains what was observed and the limits of the evidence.
- Shared identifiers link related entries. Request hashes refer to the original request bodies.
- Dates in paths use Asia/Seoul. Published timestamps use UTC.
- `null` means usage is unknown. `0` means a reported zero. Reasoning and cache counts are included in the applicable token totals. Cost estimates use the saved rates, and each live call counts once.

The public records keep content needed to reproduce the observations, including public reasoning summaries when available. They omit credentials, private reasoning, and original storage files.
