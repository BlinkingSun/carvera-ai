# From a text conversation to a finished part

With two MCP servers connected, **Autodesk Fusion's** and **Carvera AI's**, an agent can take a part
from a description to chips on the table: model, CAM, post, check, index, level and run. You stay in
the loop for the decisions and the "go".

```
you ── conversation ── agent ──┬── Fusion MCP ──── Fusion: model, CAM setup, toolpaths, post (Carvera.cps)
                               └── carvera MCP ─── Carvera Control: check, index, probe, level, upload, run
```

**Status on the reference machine:**
- **Demonstrated:** the Carvera side, steps 4–7: checking posted programs, probing, offline leveling,
  indexing from touch-offs, upload and run with the vacuum fixed.
- **Done through Fusion's MCP:** installing Makera's profiles (post, machines, tool libraries) and
  generating programs (the spoilboard program came from a generator script).
- **Supported but not yet run end to end in one conversation:** modeling and CAM authoring through
  Fusion's MCP. Fusion's Python API supports them; expect to review the toolpaths in Fusion before posting.

## Setup

1. **Fusion's MCP server.** Fusion includes a local MCP server. Enable it in Fusion → Preferences →
   General → API → "Fusion MCP Server", default port 27182. Fusion must be running with a design open.
   For Claude Code:

   ```sh
   claude mcp add --transport http --scope user fusion http://127.0.0.1:27182/mcp
   ```

   Other harnesses: register `http://127.0.0.1:27182/mcp` as a Streamable-HTTP MCP server.

2. **Makera's Fusion profiles.** Install the `Carvera.cps` post, the Carvera machine and the tool
   libraries from [MakeraInc/CarveraProfiles](https://github.com/MakeraInc/CarveraProfiles). The agent
   can install them through the Fusion API. One catch: `MachineLibrary.importMachine` treats text after the
   last dot as an extension, so pass a name ending in `.mch`.

3. **Carvera AI.** Install it as in the main README and connect to the machine.

## The steps

1. **Model.** Describe the part: dimensions, holes, pockets, fillets, material. The agent sketches,
   extrudes and patterns it through the Fusion API and shows you a screenshot. For imported geometry
   it can also open STEP files.
2. **CAM setup.** Stock from the model's bounding box plus allowances, the work origin at the stock's
   top-front-left corner (or wherever you will touch off), and toolpaths from the Carvera tool
   library: adaptive clearing, contours with finishing passes, pockets, drilling. Feeds and speeds come
   from the tool and material.
3. **Post.** Post with `Carvera.cps` to a `.cnc` file.
4. **Check before anything moves.** The agent:
   - adds the onboard vacuum. The post writes `M851 S100`/`M852`, which switch the *external* output.
     The onboard vacuum is `M801 S<pct>` / `M802`, so the agent inserts `M801 S80` after each tool change.
   - analyzes the program (`/api/program/analyze`): extents, Z range, tools.
   - replays every move against `site-rules.txt` (dowel zones, anchor keep-out) and against machine
     travel.
   - checks for nested comments, and that the first X/Y move happens at a safe height.
5. **Index.** Load the stock and push it into the anchor corner, or touch off edges with the cutting
   tool in the web UI and save points. The agent turns tool-centre touch-offs into an X/Y shift (skill
   section 5), probes Z with the wireless probe, and puts the probe back with the deep stow.
6. **Level (optional).** For thin stock or through-cuts, probe a grid with `M495` and level the program
   offline ([LEVELING.md](LEVELING.md)).
7. **Run.**
   1. Upload (XMODEM, MD5 checked).
   2. Preflight: file, operations, tools, origin, time, kill-switch envelope, vacuum.
   3. Your "go".
   4. The run plays from the SD card while the agent watches progress and the vacuum.

## Fusion Personal (hobbyist) license limits

These come from Autodesk's license, not from the posts:

- **Tool changes are refused** in a single post: two operations with different tools fail with
  "Cannot locate the simple error log generated from the Post Processing." Post one tool at a time,
  then merge the files, keeping the `T<n>M6` blocks. Or use a commercial license.
- **Rapids are slowed to the cutting feed**, and Fusion writes a "WHEN USING FUSION FOR PERSONAL USE"
  comment.
- **Tool orientation (3+2) is blocked.**

## Tips

- In the Fusion API, `cam.postProcess(collection, PostProcessInput)` raises with the post's own error
  text. `NCProgram.postProcess()` only returns False.
- Keep CAM Z0 at the stock top and say so in the setup. The post header ("Z Origin Set To: Stock Top",
  "Stock Height") tells the agent where the spoilboard is in the program's frame, which the dowel rule
  needs.
- A through-cut usually goes 0.5–1 mm into the spoilboard. Make sure the tool path stays out of the
  dowel zones.
