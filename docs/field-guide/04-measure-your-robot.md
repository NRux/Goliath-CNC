<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
# Field guide 04: measure your robot (rung R0b, robot switched off, about 2 to 3 hours)

**Goal:** with a tape measure, a square and some carbon paper, measure the few
dimensions of your Goliath that the safety checker of `goliath-prep` otherwise
has to guess. The robot stays **switched off and unplugged** the whole time.
Nothing is connected and nothing is sent, so nothing in this guide can make the
robot move. You can do it on any day, before or after the probe of
[field guide 02](02-first-probe.md).

`goliath-prep` and `goliath-run` are **not released yet**
([field guide 03](03-install-the-applications.md)). Where this guide says what
"the checker" does with your numbers, it describes how they are designed to use
them; nothing reads your numbers today (step 15).

**What it settles** (the codes refer to the design's list of unknowns and its
questions to you):

- **U-22**: what the three support points of the vendor's footprint model
  physically are, how wide a wheel touches the panel, and the body outline
  ([spec 08 §Footprint model](../protocol/08-head-angle-and-cut-order.md));
- **U-49**: where the towers' measuring wires attach to the robot (above the
  spindle or beside it, on a swivel or fixed);
- **Q7**: what your physical stop is and what it switches off, and whether the
  router has its own switch;
- **Q16**: the question that asked you for exactly these measurements.

> **The number that matters most: where, and how wide, each wheel touches the
> panel.** The checker keeps a circle of radius *wheel contact radius + 15 mm*
> around each wheel clear of fresh cuts. While the radius is unmeasured it assumes
> 40 mm, a 55 mm circle. Your measured radius replaces the guess: if it is
> smaller, more shapes and more robot headings near the limits pass the same
> checks; if it is larger, the checker gets stricter, as it should.
>
> **What measuring will not change.** At first the checker refuses every closed
> shape larger than about 200 mm across. That limit does not come from the 40 mm
> guess: it comes from holding one robot heading all the way around a closed
> outline. Once the shape is wider than about the wheels' distance from the bit
> (211 mm for the side wheels), the path of some wheel crosses the outline after
> the bit has already cut it there. Our own computation finds the same limit with
> a contact radius as small as 15 mm, for every heading, direction and starting
> point we tried. (That is a calculation, not a measurement on a robot.) Larger
> closed parts will need one of two later features: cutting the outline in
> segments at different headings, which leaves small joint marks, or a test on a
> scrap panel showing that a wheel can safely cross a fresh cut (your question
> Q15).

Write down what you measure, not what you expect. If a number is far from the
vendor's value (step 13), measure it again, and if it is still far, keep it: that
is a finding, not a mistake.

## Safety first

- **Unplug the robot** (pull its mains plug) before you start, and keep it
  unplugged until you have finished. Switch the towers off.
- **Remove the bit.** Nothing turns and nothing is cut in this guide.
- **Never lift or carry the robot by the spindle**, the router, the bit holder,
  the spindle cage or the measuring wires. Lift it by its body, the way you put
  it on a panel for a job. Published weights range from about 10 to 25 kg
  depending on the model, so lift it with a second person.
- **Keep your fingers clear of the bit holder** (the collet). The only time you
  go near it is step 3, with the robot unplugged, and then only with the marking
  rod or thread.
- **Don't open the housing.** Everything here is measured or looked at from
  outside.
- **Don't switch the robot on to check something.** Whatever needs power waits
  for a later rung, which has its own guide.
- **Don't pull a measuring wire out of its tower.** The wires are spring-loaded;
  you only look at the robot's end of them.

## What you need

- A flat panel or table at least 1 m x 1 m that the robot can stand on.
- Paper to cover about 800 x 800 mm (lining paper, or flipchart sheets taped
  together), and masking tape.
- Carbon paper, six sheets or more (a stationery shop has it). Alternative: dark
  paper with a thin, even dusting of chalk or talc; a wheel leaves a clear print
  where it presses.
- A tape measure, a steel rule of 300 mm or more, and callipers if you have them.
- A carpenter's square (or any large set square) and a long straightedge (a 1 m
  rule or a straight batten).
- A sharp pencil and a fine marker.
- For step 3: a straight round rod that slides into the collet (for example a
  wooden dowel of the collet's size, 6 mm, 8 mm or 1/4 inch, about 150 mm long,
  sharpened to a point), or a thread with a small pointed weight (a plumb bob).
- A phone camera, and a calculator with arctan (shown as tan⁻¹ or atan; the
  phone's calculator has it in its scientific mode).
- A printed copy of the fill-in table (step 14), or a notebook.
- A helper for lifting.

## How positions are written (the body frame)

Every position is measured **from above**, on the sheet under the robot, from one
point and along one line:

- **`T`**, the origin: the point on the sheet straight under the centre of the
  collet, marked in step 3.
- **The body axis:** the robot's centre line, from the spindle end (the front) to
  the back, drawn in step 6.
- **back** (mm): how far a point lies **behind** `T`, measured along the body
  axis. A point ahead of `T` gets a minus sign.
- **right** (mm): how far a point lies to the **right** of the body axis, measured
  square to it. Right means: seen from above, standing behind the robot and
  looking toward the spindle. A point to the left gets a minus sign.
- **distance** (mm): the straight distance from `T` to the point.
- **angle** (degrees): the angle at `T` between the body axis (pointing back)
  and the line to the point; positive to the right, negative to the left. Work it
  out as **angle = arctan(right ÷ back)**. Example: right = 204.7, back = 51.0
  gives arctan(4.01) = 76.0°.

```text
                  front (spindle end)
                          T                 <- collet mark, the origin
     q3 (left) x----------F----------x q2 (right)    side wheels, about 51 mm behind T
                          |
                          |  body axis ("back" grows downward)
                          |
                          x q1                        rear wheel, about 402 mm behind T
```

The names q1, q2 and q3 are those of spec 08. In this frame, the vendor's model
puts the wheel modules here (spec 08 §Footprint model and §Constants):

| Module | Name | back | right | distance | angle |
|---|---|---|---|---|---|
| rear | q1 | 402 | 0 | 402 | 0° |
| right side | q2 | 51 | +205 | 211 | +76° |
| left side | q3 | 51 | -205 | 211 | -76° |

Spec 08 places q2 at +76° and q3 at -76°. Seen from above with the panel's X
axis pointing to the right, its formula puts q2 on the robot's right, so this
guide calls the right side module q2. Whether the robot really uses that
orientation, and not its mirror image, is not known yet (U-20; rung R3 checks
it). You don't need to decide that: you name the modules by where they physically
are, and mapping them is the software's job.

## 1. Prepare the sheet (10 minutes)

1. Write the date and your name or initials in the table (`session.date`,
   `session.operator`).
2. Make the panel or table clean and level, and tape the paper flat on it.
3. With your helper, set the robot on the paper, near the middle. Draw roughly
   around each wheel module with the pencil, so you know where they stand. Lift
   the robot off again.
4. Over each of the three outlines, tape a whole sheet of carbon paper, **carbon
   side down** (toward the paper), centred on the outline. Tape only its edges.

## 2. Take the wheel prints (5 minutes)

1. Set the robot down gently so that each wheel module stands on its carbon sheet.
2. Press gently with your hand on the body above each wheel module for a few
   seconds. Don't hit it.
3. Leave the robot standing there for steps 3 and 4.

Where a wheel touches, it presses the carbon onto the paper. (If the prints come
out faint, press a little longer, or use the chalk alternative.)

## 3. Mark the collet axis (10 minutes)

The robot is still unplugged, with the bit removed. Open the spindle cage the way
you do to change a bit.

- **With a rod:** slide the rod up into the collet, then let it slide back down
  until its point rests on the paper, with at least 15 mm of the rod still inside
  the collet. Tighten the collet nut only finger-tight, so the rod hangs straight.
  Mark the paper right around the point, then loosen the nut and take the rod out.
- **With a plumb line:** plug the collet's opening with a short piece of dowel
  that fits it, pin or tape the thread to the centre of the dowel's end, and let
  the weight hang just above the paper. When it has stopped swinging, mark the
  paper straight under its point. Take the dowel out afterwards.

Write `T` next to the mark (you will see it when the robot is lifted off). Close
the spindle cage.

## 4. Outline, wheel ends, wire point and photos from above (20 minutes)

The robot still stands on the sheet.

1. **Body outline.** Go round the robot with the square standing on the paper:
   touch the outermost part of the body with the square's upright edge and mark
   the paper at the square's foot. Do this every 50 mm or so all round, and at
   every corner. Parts that stick out higher up (handles, the cage, cable glands)
   count: the outline is what you would see from straight above.
2. **Wheel ends.** For each wheel module, stand the square so that its upright
   edge just touches the outermost roller at one end of the module's axle (a
   roller, not a hub cap or a bracket), and mark the square's foot; do the same at
   the other end. Label the marks, for example `q1 end a` and `q1 end b`. If you
   cannot reach an end, skip it and say so in `wheel.notes`.
3. **Wire point.** Find where the towers' measuring wires attach to the robot
   (reports say on top of the machine, with magnetic connections: §4 of the
   [prior-art review](../research/prior-art-review.md), "PAR" below). Hang
   the plumb line from that point, or stand the square against it, and mark the
   paper straight below it: `W`. If neither can reach the paper from there, leave
   `W` out; step 9 says what to write. If you can't find the point now, look
   again in step 9.
4. **Photos from above.** Lay the tape measure or a long rule flat on the paper
   beside the robot, so the photo has a scale. Stand on a stable step and hold
   the phone flat (screen parallel to the floor) over the middle of the robot.
   Take three or four photos. Keep any serial-number label out of the picture.
5. With your helper, lift the robot straight up off the paper (by the body, see
   *Safety first*) and set it down on a stable surface nearby.

## 5. Rolling bands (optional, 15 minutes)

A wheel module touches the panel in slightly different places as its rollers
turn. Rolling the robot a short way shows all of them. The bands go on separate
carbon sheets, so that they don't cover the still prints of step 2.

1. On a second sheet of paper (or a clear part of the first), tape fresh carbon
   paper, carbon side down, where the three modules will stand, as in step 1. Set
   the robot down on them.
2. Push the robot **gently**, by its body, toward the spindle end. If it does not
   roll with light hand force, **stop**: don't force it (the wheel gearboxes may
   not be meant to turn by hand). Write `no` in `wheel.rolls_by_hand`, lift the
   robot off and skip the rest of this step.
3. If it rolls: push it slowly, a few centimetres per second, about 80 mm toward
   the spindle end and back to where it was; then about 80 mm to one side and
   back. Keep it well away from the edge of the table. Write `yes` in
   `wheel.rolls_by_hand`.
4. Lift the robot off. Each module has drawn bands on its carbon sheet: take the
   carbon off and label the bands q1, q2, q3 (rear, right, left, as in step 6).

## 6. Draw the body axis and measure the wheel positions (30 minutes)

Take the carbon sheets off the first sheet. On it you now have `T`, the still
prints, the wheel-end marks, the outline dots and maybe `W`.

1. **Contact centres.** For each module, mark a small cross at its contact
   centre:
   - with both end marks: halfway between them, on the line of the prints; write
     `end_marks` in `footprint.centre_method`;
   - without them: the middle of that module's whole group of prints; write
     `middle_of_marks`.

   Label the crosses: **q1** the rear module (farthest from `T`), **q2** the
   side module on the right, **q3** the one on the left (right and left as seen
   standing behind the robot, looking toward the spindle).
2. **Body axis.** Draw a straight line through the q2 and q3 crosses, long enough
   to pass beside `T`. Put the square on this line and slide it until its other
   edge passes through `T`; draw that edge as a long line through `T`, on past q1.
   This is the body axis. Mark `F` where it crosses the q2-q3 line.
3. **Check the axis.** Does it run along the middle of the outline dots, and of
   the body in your photo from above? Write `yes`, `no` or `unknown` in
   `footprint.axis_matches_body`. A `no` or `unknown` is a no-go in step 13:
   the angles would then be measured about a line that may not be the robot's
   centre line.
4. **Side modules.** `footprint.q2_back_mm` and `footprint.q3_back_mm` are both
   the distance from `T` to `F`. `footprint.q2_right_mm` is the distance from `F`
   to the q2 cross; `footprint.q3_right_mm` is the distance from `F` to the q3
   cross, **with a minus sign**.
5. **Rear module.** Put the square on the axis and slide it until its edge passes
   through the q1 cross; mark the foot `G` on the axis. `footprint.q1_back_mm` is
   `T` to `G`; `footprint.q1_right_mm` is `G` to the cross (0 if the cross is on
   the axis, minus if it is to the left).
6. **Wire point.** If you marked `W`, measure its back and right the same way
   (`wire.back_mm`, `wire.right_mm`).
7. **Straight distances.** With the tape, measure from `T` to each cross
   (`footprint.q1_mm`, `footprint.q2_mm`, `footprint.q3_mm`) and between the
   crosses (`footprint.q1_q2_mm`, `footprint.q2_q3_mm`, `footprint.q3_q1_mm`).
8. **Angles.** For each module: angle = arctan(right ÷ back). q3's comes out
   negative because its right is negative (`footprint.q1_deg`,
   `footprint.q2_deg`, `footprint.q3_deg`). If a cross lies ahead of `T` (its
   back is negative, far from the vendor's model), the angle's size is 180° minus
   arctan(right ÷ back) taken without signs, and it keeps the sign of right; note
   it in `notes.general`.
9. **Quick check.** For each module, √(back² + right²) on the calculator should
   be within 3 mm of the straight distance you taped. If not, measure both again.
10. Photograph the whole sheet flat, with the rule on it (`footprint.photos`).
    Keep the sheet: roll it up, don't fold it. It is your raw record.

Write distances to the millimetre, as you read them on the tape, and angles to
one decimal place (for example 75.8°). Don't round them any further: step 13
compares them exactly as written, and rounding can move a value that is just
outside its range to inside it.

## 7. The wheel modules: size, rollers, contact extent and radius (20 minutes)

Look at each module from the side and, if you can, from below. Photograph each
one with a rule in the picture (`wheel.photos`). For each of q1, q2 and q3:

1. **Wheels in the module** (`wheel.q1.wheels_in_module`, and the same for q2,
   q3): how many separate wheels. One report speaks of nine wheels in three sets
   (PAR §4).
2. **Diameter** (`wheel.q1.diameter_mm`): across the wheel at its widest, over
   the rollers.
3. **Width** (`wheel.q1.width_mm`): along the axle, from the outer face of the
   outermost roller on one side to the outer face of the outermost roller on the
   other side. It should match the distance between the module's two end marks
   on the sheet; if it doesn't, measure both again.
4. **Roller rows** (`wheel.q1.roller_rows`): how many rows of rollers across the
   whole module, all its wheels together. **Row spacing**
   (`wheel.q1.row_spacing_mm`): centre to centre between two neighbouring rows
   along the axle; the largest if they differ, 0 for a single row.
5. **Contact extent** (`wheel.q1.contact_extent_mm`): the longest dimension of
   the module's still print (step 2), or the width of its widest rolling band
   measured **across** the direction of rolling (step 5), whichever is larger.
   The length of a band only shows how far you pushed: don't use it.
6. **Contact radius** (`wheel.q1.contact_radius_mm`): the largest of these
   three numbers, **rounded up** to the next whole millimetre:
   - the distance from the module's cross to the outer edge of its still print;
   - half the width of its widest rolling band (if you made them);
   - half the module's width (item 3).

   It is the radius of a circle around the cross that holds every place the
   module can touch.

The design uses one contact radius for all three modules, so the largest of your
three is the value that keeps things safe. Anything unusual (wheels of one module
that don't share an axle, worn or damaged rollers) goes in `wheel.notes`.

## 8. Body outline numbers (10 minutes)

Join the outline dots on the sheet into a line. Then, with the square on the body
axis:

- `body.ahead_of_tool_mm`: from `T` to the foot of the square at the dot
  farthest toward the front;
- `body.behind_tool_mm`: the same at the dot farthest toward the back;
- `body.left_of_axis_mm` and `body.right_of_axis_mm`: the longest square
  distances from the axis to the outline on each side (both as positive numbers);
- `body.max_radius_mm`: the longest straight distance from `T` to any outline
  dot (try the few that look farthest).

Optional: `body.outline_points`, the outline as back,right pairs going round
the body. The photo of the sheet is enough for now. Photograph the traced outline
with a rule on it (`body.photos`).

## 9. Where the measuring wires attach (U-49, 10 minutes)

1. Look at where each tower's wire attaches to the robot, from the side and from
   above. Photograph it close up, from the side and from above, with a rule in
   the picture (`wire.photos`). Don't pull the wires out of the towers.
2. `wire.attachment`: `on_spindle_axis` only if `W` is within 2 mm of `T` on the
   sheet; `offset` if it is anywhere else; `not_found` if you could not find an
   attachment point; `unknown` if you found it but could not mark `W` (for
   example, nothing can hang straight down from it). In that last case also write
   `unknown` for `wire.back_mm` and `wire.right_mm`, and describe where it is in
   `wire.notes`, with a photo.
3. `wire.swivel`: turn the part the wires attach to gently by hand. If it turns
   freely about an upright axis, relative to the body, write `swivel`; if not,
   `fixed`. Don't force it.
4. `wire.same_point`: do both towers' wires attach at the same point? If not,
   describe both points in `wire.notes`; that case goes to design review.
5. Optional, `wire.height_mm`: height of the attachment point above the surface
   the robot stands on.
6. In `wire.notes`: how the wires attach (magnetic, a clip, something else) and
   anything that would stop the body turning freely under them.

If you found the attachment point only now, put the robot back on its prints on
the sheet, mark `W`, lift the robot off again, and measure `W` as in step 6.

## 10. Tower base (5 minutes)

Measure the base of each tower, the part that stands on or beside the panel.
Write the larger values if the two towers differ.

- `tower.base_length_mm` and `tower.base_width_mm`: the overall size of the base.
- `tower.base_radius_mm`: from the point straight below where the wire leaves the
  tower to the farthest edge of its base.
- Photograph each tower from above and from the side, with a rule
  (`tower.photos`). Keep any serial-number label out of the picture.

## 11. The physical stop, the controller switch and the router switch (Q7, 10 minutes)

Look, don't open anything. The robot stays unplugged.

1. **The stop** (`stop.kind`, `stop.location`): what will you use to stop the
   robot in an emergency? An emergency-stop button on the robot
   (`estop_button_on_robot`), a power switch on the robot
   (`power_switch_on_robot`), the mains plug (`mains_plug`), a switched socket or
   power strip (`switched_socket_or_strip`), a separate emergency-stop switch in
   the mains supply (`separate_mains_estop`), something else (`other`), or
   nothing found (`none_found`). Describe where it is.
2. **Does it latch?** (`stop.latching`): press it. Does it stay off until you
   deliberately twist or pull it back? Pressing it with the robot unplugged is
   harmless. Reset it afterwards.
3. **What does it switch off?** (`stop.cuts`): the controller's power
   (everything, including the Wi-Fi), only the motors and the spindle, or only the
   spindle. You usually can't see this from outside: write what a label, the
   manual or your experience tells you, and which of those it is
   (`stop.cuts_basis`). If nothing tells you, write `unknown`. A later rung (R2)
   checks it with power on.
4. **The controller switch** (`power.controller_switch`): which switch turns the
   robot's electronics, including its Wi-Fi, off? It may be the same as the stop.
   You will use it for every power cycle between jobs.
5. **The router's own switch** (`router.own_switch`, `router.switch_location`):
   does the router, the spindle motor, have its own on/off switch? (One report
   says the spindle is a re-skinned DeWalt 611 router, PAR §4; such routers
   normally have their own switch.) The first motion rungs keep it off.
6. **Run-down time** (`spindle.rundown_s`, `spindle.rundown_basis`): if you
   know from experience or the manual how long the router takes to stop turning
   after its power is cut, write it and how you know. Don't switch anything on to
   find out.

**Recommended (Q7):** before any rung in which the robot moves, have a latching
emergency-stop switch in the robot's mains supply, within reach of where you will
stand. The robot's firmware probably has no software stop. This is your decision.

## 12. Repeat the positions once (recommended, 40 minutes)

On a fresh sheet, with the robot set down in a different spot and turned a
different way, do steps 1 to 4 and 6 again (the outline, wire point and photos
can be skipped). Compare every footprint distance with the first run and write
the largest difference in `footprint.repeat_max_diff_mm`. If any distance differs
by more than 5 mm, find out why before you go on. Write the average of the two
runs in the table, with its decimal (an average such as 401.5 stays 401.5). If
you measure only once, write `unknown` in
`footprint.repeat_max_diff_mm`.

## 13. Check against the vendor's model (go / no-go)

Compare your numbers with the vendor's (design 11.2, rung R0b):

| Field | Vendor | Your value must be within |
|---|---|---|
| `footprint.q1_mm` | 402 mm | 387 to 417 mm |
| `footprint.q2_mm` | 211 mm | 196 to 226 mm |
| `footprint.q3_mm` | 211 mm | 196 to 226 mm |
| `footprint.q1_deg` | 0° | -5° to +5° |
| `footprint.q2_deg` | +76° | +71° to +81° |
| `footprint.q3_deg` | -76° | -81° to -71° |

Compare each value exactly as you wrote it in the table, without rounding it:
81.4° is outside +71° to +81°, and 417.5 mm is outside 387 to 417 mm.

- **Go:** all six are within range, and `footprint.axis_matches_body` is `yes`
  (step 6). The vendor's footprint model describes your robot to within
  tape-measure accuracy. The checker is designed to use both models at once:
  every wheel must be safe under the vendor's numbers and under yours.
- **No-go:** a value is outside its range, or `footprint.axis_matches_body` is
  `no` or `unknown` (then the angles were measured about a line that may not be
  the robot's centre line). Measure again from step 1 on a fresh sheet. If it is
  still a no-go, **don't change any value**: write it down, send the table and
  photos back, and stop the bring-up here until a design review has looked at it
  (design 11.1). The checker keeps using both models in any case.

Write anything unexpected, and anything you could not measure and why, in
`notes.general`.

## 14. Fill-in table

Write a value or the word `unknown` on every line. Never leave a line empty and
never guess: `unknown` keeps the software's cautious default for that item, which
is the safe answer. In the ± column, write how far off you think your value could
be (for example 2 mm). The same ids, with their meaning, how each is measured and
what it feeds in the design, are listed in
[`04-measure-your-robot.fields.json`](04-measure-your-robot.fields.json).

### Session

| Id | What | Unit or answer | Your value | ± | Vendor model |
|---|---|---|---|---|---|
| `session.date` | date of the measurement | text | | | |
| `session.operator` | your name or initials | text | | | |

### Wheel positions (body frame)

| Id | What | Unit or answer | Your value | ± | Vendor model |
|---|---|---|---|---|---|
| `footprint.q1_back_mm` | rear module: behind `T` | mm | | | 402 |
| `footprint.q1_right_mm` | rear module: right of the axis (minus = left) | mm | | | 0 |
| `footprint.q1_mm` | rear module: straight distance from `T` | mm | | | 402 ± 15 |
| `footprint.q1_deg` | rear module: angle (plus = right) | deg | | | 0 ± 5 |
| `footprint.q2_back_mm` | right module: behind `T` | mm | | | 51 |
| `footprint.q2_right_mm` | right module: right of the axis | mm | | | 205 |
| `footprint.q2_mm` | right module: straight distance from `T` | mm | | | 211 ± 15 |
| `footprint.q2_deg` | right module: angle | deg | | | +76 ± 5 |
| `footprint.q3_back_mm` | left module: behind `T` | mm | | | 51 |
| `footprint.q3_right_mm` | left module: right of the axis (negative) | mm | | | -205 |
| `footprint.q3_mm` | left module: straight distance from `T` | mm | | | 211 ± 15 |
| `footprint.q3_deg` | left module: angle (negative) | deg | | | -76 ± 5 |
| `footprint.q1_q2_mm` | rear to right module | mm | | | 406 |
| `footprint.q2_q3_mm` | right to left module | mm | | | 409 |
| `footprint.q3_q1_mm` | left to rear module | mm | | | 406 |
| `footprint.axis_matches_body` | drawn axis runs along the middle of the body | `yes` / `no` / `unknown` | | | |
| `footprint.centre_method` | how the contact centres were found | `end_marks` / `middle_of_marks` / `unknown` | | | |
| `footprint.repeat_max_diff_mm` | largest difference between the two runs | mm | | | |
| `footprint.photos` | photo file names: from above, the sheet | text | | | |

### Wheel modules

| Id | What | Unit or answer | Your value | ± | Vendor model |
|---|---|---|---|---|---|
| `wheel.q1.wheels_in_module` | rear module: number of wheels | count | | | |
| `wheel.q1.diameter_mm` | rear module: wheel diameter over the rollers | mm | | | |
| `wheel.q1.width_mm` | rear module: width over the outermost rollers | mm | | | |
| `wheel.q1.roller_rows` | rear module: rows of rollers | count | | | |
| `wheel.q1.row_spacing_mm` | rear module: spacing of neighbouring rows | mm | | | |
| `wheel.q1.contact_extent_mm` | rear module: longest dimension of its marks | mm | | | |
| `wheel.q1.contact_radius_mm` | rear module: contact radius, rounded up | mm | | | |
| `wheel.q2.wheels_in_module` | right module: number of wheels | count | | | |
| `wheel.q2.diameter_mm` | right module: wheel diameter over the rollers | mm | | | |
| `wheel.q2.width_mm` | right module: width over the outermost rollers | mm | | | |
| `wheel.q2.roller_rows` | right module: rows of rollers | count | | | |
| `wheel.q2.row_spacing_mm` | right module: spacing of neighbouring rows | mm | | | |
| `wheel.q2.contact_extent_mm` | right module: longest dimension of its marks | mm | | | |
| `wheel.q2.contact_radius_mm` | right module: contact radius, rounded up | mm | | | |
| `wheel.q3.wheels_in_module` | left module: number of wheels | count | | | |
| `wheel.q3.diameter_mm` | left module: wheel diameter over the rollers | mm | | | |
| `wheel.q3.width_mm` | left module: width over the outermost rollers | mm | | | |
| `wheel.q3.roller_rows` | left module: rows of rollers | count | | | |
| `wheel.q3.row_spacing_mm` | left module: spacing of neighbouring rows | mm | | | |
| `wheel.q3.contact_extent_mm` | left module: longest dimension of its marks | mm | | | |
| `wheel.q3.contact_radius_mm` | left module: contact radius, rounded up | mm | | | |
| `wheel.rolls_by_hand` | robot rolls when pushed gently, power off | `yes` / `no` / `unknown` | | | |
| `wheel.photos` | photo file names: modules and marks | text | | | |
| `wheel.notes` | anything unusual about the wheels | text | | | |

### Body

| Id | What | Unit or answer | Your value | ± | Vendor model |
|---|---|---|---|---|---|
| `body.ahead_of_tool_mm` | outline ahead of `T` | mm | | | |
| `body.behind_tool_mm` | outline behind `T` | mm | | | |
| `body.left_of_axis_mm` | outline left of the axis | mm | | | |
| `body.right_of_axis_mm` | outline right of the axis | mm | | | |
| `body.max_radius_mm` | farthest outline point from `T` | mm | | | |
| `body.outline_points` | optional: outline as back,right pairs | text | | | |
| `body.photos` | photo file names: from above, traced outline | text | | | |

### Measuring-wire attachment

| Id | What | Unit or answer | Your value | ± | Vendor model |
|---|---|---|---|---|---|
| `wire.attachment` | where the wires attach | `on_spindle_axis` / `offset` / `not_found` / `unknown` | | | |
| `wire.swivel` | the attachment turns freely, or not | `swivel` / `fixed` / `unknown` | | | |
| `wire.same_point` | both wires attach at one point | `yes` / `no` / `unknown` | | | |
| `wire.back_mm` | attachment point `W`: behind `T` | mm | | | |
| `wire.right_mm` | attachment point `W`: right of the axis | mm | | | |
| `wire.height_mm` | optional: height above the surface | mm | | | |
| `wire.photos` | photo file names: the attachment | text | | | |
| `wire.notes` | how they attach; anything limiting turning | text | | | |

### Towers

| Id | What | Unit or answer | Your value | ± | Vendor model |
|---|---|---|---|---|---|
| `tower.base_length_mm` | base length | mm | | | |
| `tower.base_width_mm` | base width | mm | | | |
| `tower.base_radius_mm` | wire exit to the farthest edge of the base | mm | | | |
| `tower.photos` | photo file names: both towers | text | | | |

### Stop and switches

| Id | What | Unit or answer | Your value | ± | Vendor model |
|---|---|---|---|---|---|
| `stop.kind` | what the physical stop is | `estop_button_on_robot` / `power_switch_on_robot` / `mains_plug` / `switched_socket_or_strip` / `separate_mains_estop` / `other` / `none_found` / `unknown` | | | |
| `stop.location` | where it is | text | | | |
| `stop.latching` | stays off until reset | `yes` / `no` / `unknown` | | | |
| `stop.cuts` | what it switches off | `controller_power` / `motors_and_spindle_only` / `spindle_only` / `unknown` | | | |
| `stop.cuts_basis` | how you know | `label` / `manual` / `experience` / `unknown` | | | |
| `power.controller_switch` | the switch that turns the controller off, and where | text | | | |
| `router.own_switch` | the router has its own switch | `yes` / `no` / `unknown` | | | |
| `router.switch_location` | where it is, or `none` | text | | | |
| `spindle.rundown_s` | optional: router run-down time | s | | | |
| `spindle.rundown_basis` | how you know the run-down time | `timed` / `manual` / `estimate` / `unknown` | | | |

### Notes

| Id | What | Unit or answer | Your value | ± | Vendor model |
|---|---|---|---|---|---|
| `notes.general` | anything unexpected or not measured, and why | text | | | |

## 15. What happens to these numbers

- **Nothing in the software reads them yet.** The runner's measurement entry
  (planned: `goliath-run measure R0b`, not released yet; see
  [field guide 03](03-install-the-applications.md)) will ask for them by the ids
  above, with the unit, your ± and your name, and store them as measured values.
  A repository test checks that its ids and this guide's stay the same.
- **Until you have entered them,** the checker keeps its cautious defaults (wheel
  contact radius 40 mm; body = the outline of the wheel circles + 60 mm; wire
  attachment = anywhere on the body; tower base = a 120 mm radius) and shows the
  amber banner UNMEASURED MACHINE PROFILE (design 4.5, U-22).
- **Don't type these numbers into a machine profile file by hand.** A hand-edited
  profile is loaded as an owner profile, shown as such, and does not count as
  measured (design 3.1).
- **Send back,** privately, the filled table and the photos, as you did with the
  probe results. Before you send them, check that no photo shows a serial-number
  label of the robot or the towers.
