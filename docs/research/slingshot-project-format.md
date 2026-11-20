<!-- SPDX-License-Identifier: GPL-3.0-or-later -->

# Slingshot project formats and preservation

This document describes the project data used by **Slingshot 1.9.3.0**, from
static inspection on 2026-11-06. Its purpose is to support an independent,
offline importer that preserves owners' projects. It is not a specification for
rebuilding the vendor's geometry engine or evidence that an imported job is safe
to run.

The inspected installers and extracted files are identified by SHA-256 in the
[artifact manifest](slingshot-artifact-manifest.json). The broader scope and
limitations are in the [reverse-engineering report](slingshot-reverse-engineering.md).
Source locators below refer to local ILSpy 9.1.0.7988 output under
`.local-research/decompiled/1.9.3.0/`; that recovered source is not published.
Line numbers describe this extraction and may change with another decompiler.
No vendor application, serializer, or generated job was executed.

## What was observed, and what remains inferred

The installer contains one inspected calibration work area at
`app/Calibration/CenterBitCalibration/Canvas.glth`. It is ordinary XML with a
UTF-8 declaration, an SVG root, and custom machining attributes. A standard XML
parser read it without executing vendor code or fetching its declared external
SVG DTD. Its root has the six fields for PPI, material, panel dimensions, and
title listed below. Its operation elements contain twelve distinct `QI_*`
fields, including tool, operation type, compensation, depth, and feed settings.

The project-manifest schema and reader/writer behavior below come from static
host methods. No real owner-created `project.prj`, `.qcm`, or `.ute` file was
supplied or round-tripped. The calibration sample establishes one actual `.glth`
representation; it does not establish every user's project schema or that all
five Slingshot versions use identical formats.

## Format map

| Artifact | Representation and role | Evidence |
|---|---|---|
| Project directory | Contains a manifest and separate work areas. It is not a single encrypted project container. | `Slingshot/GoliathWPF.Helpers/Project.cs`, `Save` line 202 and `Load` line 237. |
| `project.prj` | XML serialized from `Project`. Properties include `Titolo`, `DataCreazione`, `DataModifica`, and `WorkAreasSerialized`. The latter is a pipe-delimited sequence of work-area basenames with a trailing separator when nonempty. | Same file, `WorkAreasSerialized` line 117, `Save` line 202, `Load` line 237. |
| `.glth` | SVG XML with custom attributes for machining and panel settings. `SvgDocument.Open` reads it and `SvgDocument.Write` saves it. | `Slingshot/GoliathWPF.Helpers/WorkArea.cs`, `SvgPath` line 241 and `Save` line 836; packaged sample above. |
| `.qcm` | XML serialization of an intermediate `LavorazioniContainer`, written after planning. The general reader also has a gzip-to-XML branch; the numeric header test remains obfuscated and was not decoded here. | `Slingshot/GoliathWPF/Editor.cs` line 569; `SysLavGest/SysLavGest.Lavs/LavorazioniContainer.cs`, `LoadFromFile` line 23835 and `SaveToFile` line 23894. |
| `ppGoliath.ute` | Tool library serialized with .NET `BinaryFormatter`. The host creates default tools when no tools load. No `.ute` sample was found in the extracted 1.9.3.0 installer. | `Slingshot/GoliathWPF.Helpers/GoliathStatus.cs`, `CaricaFrese` line 510; `SysLavGest/SysLavGest/Utensili.cs`, `Load` line 144 and `Save` line 203. |
| `.NC` | Generated job text, distinct from editable project data. Before upload, the host strips semicolon comments, preserves line boundaries, and ensures a trailing blank line. | `PPGoliath/PPGoliath/PPGoliath.cs`, `PPGenerate` line 881; `Slingshot/GoliathWPF.Helpers/GCodeCleaner.cs`, `GetGCodeForGoliath` line 9. See the [G-code description](../protocol/05-gcode-dialect.md). |

The manifest loader temporarily changes the process working directory to resolve
work-area filenames. It uses their basenames, skips missing files, and falls back
to enumerating `*.glth` when no `project.prj` exists. A replacement should retain
the same file association without changing process-wide state or silently
discarding missing work areas. Dates displayed after loading are replaced from
directory timestamps; they are not necessarily the original XML dates.

## Custom SVG fields

These are XML attribute values, not binary fields. `CAManager` parses numeric and
Boolean values through .NET conversions and enums through case-insensitive enum
parsing, with invariant culture configured. The writer uses the value's string
representation. Missing or unparseable values can silently become type defaults
in the vendor reader. That is a compatibility observation, not an acceptable
default for approving a machining job.

| Exact field | Location | Representation and meaning |
|---|---|---|
| `QI_TITOLO` | SVG root | Text title; the host can fall back to the filename. |
| `QI_CODICE_MATERIALE` | SVG root | Text material identifier. It is not a verified material capability or cutting prescription. |
| `QI_PPI` | SVG root | Integer pixels per inch used by the SVG reader; the host substitutes 96 when zero. |
| `QI_LARGHEZZA_PANNELLO`, `QI_ALTEZZA_PANNELLO`, `QI_SPESSORE_PANNELLO` | SVG root | Numeric panel width, height, and thickness, in millimetres in the host's work-area model. These are separate from SVG viewport dimensions. |
| `QI_TIPO_LAVORAZIONE` | Operation element | Enum text: `None`, `Foro` (drill), `Fresata` (profile), `Scavo` (pocket). Numeric enum parsing is possible in the host; preserve unfamiliar values as unsupported. |
| `QI_COMPENSAZIONE` | Operation element | Enum text: `Nessuna`, `Interna`, `Centro`, `Esterna` (none, inside, center, outside). This requests compensation; it does not describe a compensated toolpath by itself. |
| `QI_UTENSILE` | Operation element | Text key into the tool library. A name alone does not recover the tool's geometry or validated feeds. |
| `QI_PROFONDITA` | Operation element | Numeric requested cut depth, compared with panel thickness and tool length; millimetres in this host model. |
| `QI_DIAMETRO_UTENSILE`, `QI_LARGHEZZA_LAVORAZIONE` | Operation element | Numeric tool diameter and machining width; millimetres in the geometry model. |
| `QI_DEPTH_PER_PASS` | Operation element | Numeric depth-per-pass setting, in millimetres in the host model. |
| `QI_FEED_RATE`, `QI_PLUNGE_RATE` | Operation element | Numeric work-feed and entry-feed settings. The fields flow into machining operations; their complete unit/conversion path was not traced in this format pass. Retain the original values and require an explicit unit interpretation before use. |
| `QI_CUSTOM_SETS` | Operation element | Boolean selecting custom machining settings. The host uses the stored feeds only when this is enabled. |
| `QI_PERCORSO_CHIUSO` | Operation element | Boolean closed-path flag. Geometry should independently confirm closure. |
| `QI_CANCELLATO` | Operation element | Boolean deleted flag. Deleted elements may remain in the SVG; the loader excludes them from cuts. |
| `QI_BRIDGES` | Operation element | Text records shaped `B<element-index> <distance> <total-distance> `; the reader also accepts records with only index and distance. Index is integer; distances are numeric. Their coordinate basis and units remain unverified here. The reader uses these only for closed, through-cut operations. This field was not present in the inspected calibration sample. |

Evidence: `Slingshot/GoliathWPF.Helpers/CAManager.cs`, enums/constants at lines
11–77, conversion at 79–107, and setters at 145–174; `WorkArea.cs`, properties at
89–179 and PPI handling at 283–287; `Cut.cs`, properties at 72–200, tool at 246,
closed/deleted flags at 360–380, bridges at 386–429, and custom-setting handoffs
at 2183–2185, 2206–2208, and 2229–2231; `BridgeList.cs`, `Save` at 67–76.

Additional non-`QI_*` metadata exists. `GROUP_ID` is parsed as a GUID; root fields
`TABSLOWERLENGTH`, `TABSUPPERLENGTH`, `TABSDEPTH`, and `TABSAUTODIST` are numeric
tab settings. Preserve these until their full use has been verified. The
`QI_CERCHIO` name appears as a declared constant, but this pass found no consumer
outside that declaration; it is not a confirmed active field.

## Geometry and generation boundaries

The import dialog selects SVG and defaults to 96 PPI. `ProjectManager.ImportSVG`
copies it to a `.glth` filename and initializes material, panel dimensions, and
thickness. `WorkArea.LoadCuts` traverses groups and supported SVG shape types.
An importer must preserve physical dimensions, viewport/view box, transforms,
and PPI together; treating SVG coordinates as millimetres without this context
can change the design's size. Evidence: `Slingshot/GoliathWPF/Import.cs`,
`BtnOpenFileDialog_Click` line 66; `ProjectManager.cs`, `ImportSVG` line 339;
`WorkArea.cs`, `LoadCuts` line 529; `MathH.cs`, `ToUser` line 16 and `ToMM` line 59.

The observed generation chain is:

```text
SVG work area and machining fields
  -> transformed and tool-compensated geometry (Cut.PathForPP)
  -> drill/profile/pocket operations (Cut.Lavorazione)
  -> validation and machining container (Editor.GenerateGCode)
  -> passes, bridges, pocket splitting, ordering, service moves (WorkLogic.WorkOut)
  -> postprocessor (PPGoliath.PPGenerate)
  -> .NC text -> upload cleaning
```

This chain explains why importing the SVG alone does not recover a ready-to-run
job. `Editor.GenerateGCode` checks placement, computes headings, rejects invalid
headings, checks tools and tabs, and builds the machining container. `WorkOut`
expands operations and inserts moves for tool changes and debris emptying before
calling the postprocessor. Evidence: `Cut.cs`, `PathForPP` line 279 and
`Lavorazione` line 2308; `Slingshot/GoliathWPF/Editor.cs`, `GenerateGCode` line
335; `WorkLogicLibrary/WorkLogicLibrary/WorkLogic.cs`, `WorkOut` line 1728 and
postprocessor call at 1984. The existing
[heading and ordering analysis](../protocol/08-head-angle-and-cut-order.md)
describes part of this process; neither analysis proves safe behavior on the
owner's firmware `01.00.13`.

## Requirements for an independent importer

1. Read project files as bounded data. Disable external XML entity/DTD resolution,
   scripts, and external SVG resource fetching. The bundled sample contains a
   DOCTYPE; accepting that declaration must not cause a network request.
2. Resolve work-area references against the selected project directory. Report
   missing assets, duplicate names, malformed manifests, and unsupported SVG
   constructs. Retain originals and write converted output separately.
3. Preserve raw attributes alongside parsed values and record source-file hashes.
   Report invalid or unknown values explicitly instead of inheriting the vendor's
   silent zero/false/default conversions. Maintain element IDs, operation order,
   grouping, deleted flags, and unsupported metadata for later recovery.
4. Resolve units and transforms before producing physical geometry. Check panel
   dimensions separately from viewport dimensions, and retain the requested PPI.
   Test with owner-created examples containing nested transforms, arcs, text,
   multiple work areas, and non-default machining settings.
5. Treat `.ute` as a separate migration problem. Do not deserialize arbitrary
   files with `BinaryFormatter` or load vendor assemblies to obtain their object
   graphs. A future data-only decoder needs its own bounded schema and fixtures;
   until then report unresolved tool references and allow explicit replacement.
6. Convert supported geometry/settings into preservation data or previews first.
   Machine-job export must wait for an implemented and validated safety pipeline;
   that complete pipeline does not exist yet. Retain all firmware and hardware
   release gates. Project import is not calibration, compatibility evidence, or
   permission to move.

An initial reader can preserve projects while using the existing independent
CAM integrations for toolpath generation. Reproducing Slingshot's full CAD/CAM
engine is a separate undertaking. Its dependencies include separately attributed
and obfuscated components; no vendor implementation is incorporated into this
format description.

Open validation work includes real owner-project fixtures, round-trip fidelity,
the complete `.qcm` schema and gzip header, `.ute` decoding, feed-unit tracing,
bridge coordinate semantics, fonts/text conversion, exact compensation and
pocketing behavior, and format differences between Slingshot versions.
