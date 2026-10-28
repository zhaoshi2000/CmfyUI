# AstroGate image node implementation plan

**Goal:** Add “AstroGate AI 生成图像” to ComfyUI's image menu, with connectable prompt/reference inputs and an IMAGE output.

**Architecture:** One V3 custom node calls the existing AstroGate gateway only when executed. Core ComfyUI remains untouched. Credentials come from the process environment or a private local file, never from workflow widgets.

**Tech Stack:** Python, ComfyUI V3 IO, aiohttp, Pillow, NumPy, PyTorch (all already installed).

**Spec:** User request and confirmed prompt → optional references → generated image connection in this chat. Builder decisions: one image per execution; current live model gpt-image-2; 13 website aspect ratios; original/1K/2K/3K/4K with the website's long-edge sizing (4K = 3840); low/medium/high quality. Explicit resolutions are fitted to their requested dimensions, matching the website. Re-running the node creates a new generation. No automatic retries on generation failure.

## Constraints and review focus

- All integration code lives in custom_nodes/astrogate; no new dependencies or core network code.
- The key is ASTROGATE_IMG_API_KEY or .secrets/astrogate-img-api-key; exclude local credentials from Git and restrict Windows ACLs.
- HTTPS gateway /internal/astrogate/v1/images/generations or /edits; JSON base64 references, at most 8 images / 50 MiB.
- Never forward credentials to response URLs or redirects; consume base64 directly.
- Preserve reference ordering, handle RGB/RGBA images, and return BHWC float32 RGB in [0,1].
- Fail clearly on authentication/timeout/malformed responses without printing provider bodies or keys.
- Verify optional reference sockets, prompt linking and native IMAGE downstream nodes in the running app.

## Task 1: Node implementation

- [x] Write custom_nodes/astrogate/tests/test_node.py first: schema connection contract, text generation, reference editing, portrait/landscape sizing, missing key, failed HTTP status without credential leakage/retry, invalid image response and reference limit.
- [x] Run the tests and record the expected missing-implementation failure.
- [x] Implement custom_nodes/astrogate/__init__.py with V3 schema, server-only key lookup, bounded async requests and image conversion.
- [x] Run the custom-node test suite and Ruff; inspect results.

## Task 2: Local integration and delivery

- [x] Make only this extension visible to Git while excluding .secrets; add a concise extension README and a reusable sample workflow.
- [x] Obtain only the bridge credential via the already-authorized SSH connection and save it with a restrictive local ACL; verify authenticated models without printing secrets.
- [x] Start ComfyUI and inspect /object_info/AstroGateImage.
- [x] Run one text-to-image workflow and one reference-image workflow, save output locally and inspect dimensions/results.
- [x] Review the extension independently, fix material findings, and confirm the live service plus UI registration schema.

## Execution record

- Read-only investigation completed before interruption; no implementation or key file existed when resumed.
- Ruling: continue in the current checkout because the requested deliverable is an installed node in the running local ComfyUI; no conflicting work exists. The confirmed user request already authorizes implementation and local setup.
- Node tests: 20 failed at setup because the implementation did not exist; after implementation, 20 passed. Ruff passed.
- Ruling: public nginx is down; the unchanged authenticated backend is healthy over SSH. Provide an explicit local SSH-tunnel launcher, with process-only API URL override, to complete local integration without modifying the production service.
- Subsequent site recovery: public HTTPS now forwards to the current origin through the entrance server's port 43922. Verified authenticated models over HTTPS, stopped the old local tunnel, and restarted ComfyUI with the default public API URL. The optional launcher now targets port 43922; direct-origin forwarding can explicitly use port 43921.
- Live executions: connected STRING → AstroGate → PreviewImage / SaveImage succeeded at 1024×576; LoadImage → reference socket → SaveImage succeeded at 1024×576 and visibly changed the door to red while preserving the scene. A further public HTTPS execution succeeded at 1152×2048 (9:16, 2K).
- Independent code review found no actionable issues. Final Ruff, PowerShell parser and git diff checks passed; delivered files and image metadata contain no credential, and the local key is Git-ignored.
- Example installed as user/default/workflows/AstroGate AI.json. Browser automation stopped because it could not reliably identify the browser URL; actual menu clicks and frontend workflow import were not visually verified. Backend registration, graph validation, prompt/reference links, preview and saved outputs were verified against the running server.

## Task 3: Character continuity and separate views

**Confirmed scope:** Continue from preceding images, retaining the same character, outfit and accessories. The user chose separate images for each viewpoint so each can feed later nodes.

**Design:** Extend the existing node with optional connectable `consistency` text, a `view` selector, and a socketless `generation_id` cache revision. Existing inputs stay in their order. New nodes default to revision 0 for native cache reuse; omitted revisions retain the prior always-generate API behavior. Revision -1 forces fresh generation; this is not an upstream seed. Use one shared character description and the first reference as an anchor, plus the preceding image as supplementary context. Each view still returns one standard IMAGE. Save/load is the durable way to lock an approved reference across cache clears or restarts. Prompt guidance cannot guarantee exact unseen details or identity.

- [x] Add failing tests for shared descriptions, view instructions, final prompt length, unchanged legacy payloads, cache revisions and credential-free schemas.
- [x] Extend only the existing custom node, then run its tests and Ruff.
- [x] Add an 11-node example: shared description → scene/character baseline → front → back → left; accessory detail also uses baseline/front. Each result has a SaveImage node and can connect downstream.
- [x] Independently review the node and example, restart the app, run the linked workflow, inspect generated views and verify downstream execution reuses the baseline cache.
- [x] Update concise usage documentation and install the example in the local workflow list.

### Continuity verification

- Added tests failed as expected (8 new failures, 20 existing passes), then all 28 passed. Independent review identified cross-node cache sharing; added a failing schema assertion and fixed it with native `not_idempotent=True`. All 28 tests and Ruff passed after that fix.
- Live schema exposes the optional continuity controls and socketless revision. The 11-node graph passed prompt validation. Baseline and front images generated successfully with the shared description.
- The first accessory request failed upstream with curl 56, connection closed abruptly. A simultaneous back request succeeded remotely but was lost from local execution when the sibling failed. Retrieved only that matching completed job's existing image through authorized SSH; no back regeneration or server modification. Resumed left view using LoadImage of the recovered back plus the original baseline. Retried only the confirmed failed accessory branch, which succeeded.
- Final outputs: baseline, front, back and left at 768×1024 RGB; accessory details at 1024×768 RGB. Visual inspection confirmed distinct viewpoints and retained the character, yellow scarf, navy jacket, silver left earring, brown bag and black boots; exact identity/unseen details are model-dependent.
- Re-running the completed baseline/front subgraph reused nodes 1–5 and completed in 0.02 seconds. Accessory retry and left-view continuation also reported nodes 1–5 cached, preserving the original baseline.
- Installed `AstroGate 人物连续视图.json` and a local `AstroGate 固定角色继续生成.json` variant whose baseline is a saved LoadImage, so continuing after cache loss does not redraw the character. Browser menu/import clicks remain unverified; graph registration, validation, execution, caching and saved results were checked directly.
