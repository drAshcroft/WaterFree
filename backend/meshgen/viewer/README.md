# Vendored: Google `<model-viewer>`

`model-viewer.min.js` renders GLBs for `waterfree meshgen preview` in headless
Chromium. It is vendored so previews work offline and cannot change under us.

| | |
|---|---|
| Package | `@google/model-viewer` 4.0.0 (npm) |
| Source | https://registry.npmjs.org/@google/model-viewer/-/model-viewer-4.0.0.tgz, `package/dist/model-viewer.min.js` |
| SHA-256 | `774edda21e1be2a0934e460ca5943af1fe3f88da130a9f98bd6a9d611576cacf` (checked by `backend/meshgen/preview.py`) |
| Licence | Apache-2.0 (`LICENSE-model-viewer`); the bundle also contains three.js (MIT) and lit (BSD-3-Clause), whose notices are kept inline |

To update: download the new tarball, replace the file, and update the hash here
and in `preview.py`.
