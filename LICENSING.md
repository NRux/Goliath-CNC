# Licensing

This repository is deliberately **split** across two licences, by component:
Apache-2.0 for the protocol library, for the interface descriptions that other
software can implement and for the CAM post-processors, GPL-3.0-or-later for the
applications and everything else.
The vendor files in `old_software/` are not licensed by this project at all (see
the last row and the clean-room note).

Some rows name folders that do not exist yet. Their licence is fixed here in
advance, so the first file created in them carries the right header.

| Component | Path | Licence | Why |
|---|---|---|---|
| Protocol/driver **library** | [`goliath-link/`](goliath-link/) | **Apache-2.0** | A driver's value is being everywhere. Apache-2.0 lets *any* tool — open or closed — speak to a Goliath, to keep as many abandoned machines working as possible. It also carries a contributor patent grant. |
| Interface **specification** | [`docs/protocol/`](docs/protocol/) | **Apache-2.0** | The specs describe facts about an interface (not copyrightable in substance); Apache-2.0 keeps the prose freely reusable alongside the library. |
| Job-preparation **interface descriptions**: the GI job format, the safety model, the check catalogue and the parameter table | `docs/prep/` | **Apache-2.0** | Like `docs/protocol/`: they describe a file format and a set of checks that other senders can implement, so the same checks can protect a Goliath whatever software prepares the job. |
| CAM **post-processors** (FreeCAD, Fusion, Kiri:Moto) | `posts/` | **Apache-2.0**, decided by the owner (question Q13, 2026-11-05) | Posts are glue loaded *into* third-party CAM programs, one of them proprietary (Fusion). Under the GPL, a plug-in that forms one program with a non-free host needs an added exception (GPL FAQ, "Can I apply the GPL when writing a plug-in for a nonfree program?"); Apache-2.0 lets anyone bundle them without that question. They contain no safety logic; that stays in `goliath-prep` and `goliath-run`, under the GPL. |
| Job **preparation** application | `goliath-prep/` | **GPL-3.0-or-later** | End-user software: it checks a job against the safety model and writes what the robot runs. Like all end-user software here it must never be re-closed (see the "Everything else" row). |
| Job **runner** and its local web interface | `goliath-run/` | **GPL-3.0-or-later** | End-user software: the program that drives the machine. Same reasoning as `goliath-prep/`. |
| **Field guides** | [`docs/field-guide/`](docs/field-guide/) | **GPL-3.0-or-later** (the root licence), decided by the owner (question Q13, 2026-11-05) | Owner-facing instructions for the applications; they follow the root licence. |
| **User guide** (the applications' offline help) | `docs/user-guide/` | **GPL-3.0-or-later** (the root licence), decided by the owner (question Q13, 2026-11-05) | Help text for the applications, shipped inside them; it follows the root licence. |
| Everything else: tools, tests, research notes, installer packaging, and the files at the repository root | [`tools/`](tools/), [`tests/`](tests/), [`docs/research/`](docs/research/), `packaging/`, repository root | **GPL-3.0-or-later** (see [`LICENSE`](LICENSE)) | The end-user software should never be re-closed — that is exactly what stranded Goliath owners when the vendor's proprietary app died. Copyleft also lets us reuse the GPL CNC ecosystem (grbl, FluidNC, …). |
| **Vendor installers** (third-party) | `old_software/` | **Not covered** by this repository's licences: proprietary files, copyright of their owners | Uploaded by the owner for analysis. Nothing in this repository grants any right to them. Moving them to a private archive repository is planned and awaits the owner's decision. |

**Licence texts.** The GPL-3.0 text is [`LICENSE`](LICENSE) at the root; the
Apache-2.0 text is [`goliath-link/LICENSE`](goliath-link/LICENSE). A component
published as a package of its own ships the text of its licence inside it
(`goliath-link/LICENSE` today). An Apache-2.0 folder without a `LICENSE` file of
its own (`docs/protocol/` today) is under that same Apache-2.0 text.

**SPDX headers.** Source files carry an SPDX `SPDX-License-Identifier` header
stating their licence. Prose files may carry it in an HTML comment
(`<!-- SPDX-License-Identifier: Apache-2.0 -->`); a file without a header is under
the licence of the row above that covers its folder.

**Clean-room note.** `goliath-link` and `docs/protocol/` were written from
observed interface behaviour and our own analysis, *not* by copying the vendor's
decompiled source. That keeps them genuinely ours to license as above.
`goliath-prep`, `goliath-run`, `posts/` and `docs/prep/` are written under the
same rule, from the specification in `docs/protocol/` and our own design and
research, without opening the decompiled source or the vendor installers. The raw
decompiled source is not published in this repository. The original vendor
installers, however, **are** in it: the owner uploaded them under `old_software/`
for analysis. They are third-party proprietary files, not covered by either of
this repository's licences, and packages or releases built from this repository
must not include them. Moving them out to a private archive repository is planned
and awaits the owner's decision; when they move, the `old_software/` row above
goes with them.
