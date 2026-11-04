# Licensing

This repository is deliberately **split** across two licences, by component.

| Component | Path | Licence | Why |
|---|---|---|---|
| Protocol/driver **library** | [`goliath-link/`](goliath-link/) | **Apache-2.0** | A driver's value is being everywhere. Apache-2.0 lets *any* tool — open or closed — speak to a Goliath, to keep as many abandoned machines working as possible. It also carries a contributor patent grant. |
| Interface **specification** | [`docs/protocol/`](docs/protocol/) | **Apache-2.0** | The specs describe facts about an interface (not copyrightable in substance); Apache-2.0 keeps the prose freely reusable alongside the library. |
| Everything else — the **applications** (controller, CAM), tools, and research docs | repository root | **GPL-3.0-or-later** (see [`LICENSE`](LICENSE)) | The end-user software should never be re-closed — that is exactly what stranded Goliath owners when the vendor's proprietary app died. Copyleft also lets us reuse the GPL CNC ecosystem (grbl, FluidNC, …). |

Each component carries its own `LICENSE` where it differs from the root
(`goliath-link/LICENSE` is Apache-2.0). Source files carry an SPDX
`SPDX-License-Identifier` header stating their licence.

**Clean-room note.** `goliath-link` and `docs/protocol/` were written from
observed interface behaviour and our own analysis, *not* by copying the vendor's
decompiled source. That keeps them genuinely ours to license as above. The raw
decompiled source and the original vendor installers are **not** published in
this repository.
