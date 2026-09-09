# Tag Template Syntax

This guide documents the reusable template syntax for tag commands such as
`metadata -add` and `metadata -delete`.

The default workflow is lowercase-first tagging. Examples use lowercase
namespaces and values. Optional case transforms (`lower`, `upper`, `caps`)
exist for cleanup; prefer keeping source tags lowercase.

## Where It Works

The shared template resolver applies to:

- `metadata -add`
- `metadata -delete`

Templates are resolved per item against that item's current tag set and
lightweight result fields such as the current title.

## Running Examples

Every section below refers back to these three titles. Keep them in mind while
reading, so the commands map onto something concrete.

```text
# Title A — dated podcast/lecture
10-22-16 Kabbalah For Heretics Broadcast on The Gnostic Gospels w Rebbe Yakov-Leib HaKohain

# Title B — numbered music track
06 - 6 1st stage separation

# Title C — course with a part number
ancient greek intensive course - part 9
```

## Core Placeholder Syntax

Use `$(namespace)` to insert the value from an existing namespaced tag. The
prefix is **Preferences → Tag Placeholder Prefix** (default `$`; the legacy
`#(` form still works).

```powershell
metadata -add "title:$(track) - $(series)"
metadata -add "album:$(series)"
metadata -delete "title:$(track) - $(series)"
```

If an item has:

```text
track:9
series:ancient greek intensive course
```

then `title:$(track) - $(series)` resolves to:

```text
title:9 - ancient greek intensive course
```

`$(title)` also resolves against the item's display title when no `title:` tag
exists yet, so transforms can read the title directly.

### Namespace Matching

- Namespace matching is case-insensitive.
- Repeated whitespace inside the placeholder is normalized.
- A trailing `#` is ignored for compatibility: `$(track #)` resolves the same
  way as `$(track)`.

```powershell
metadata -add "title:$(track #) - $(series)"
metadata -add "code:$(disc number)"
```

### Nth Value

When a namespace has several values, `$(ns)` joins them with `", "`. Use a
1-based index (Mp3tag `$meta(field,n)` / Picard `$meta(field,n)`):

```powershell
metadata -add "artist:$(artist, 1)"
metadata -add "guest:$(artist, 2)"
```

Negative indexes count from the end (`$(artist, -1)` is the last artist).

### Date Formatting

Use `$(date, FORMAT)` (tokens: `YYYY`, `YY`, `MM`, `DD`, `MMM`, `MMMM`). If
you omit the format, **Preferences → Date Format** is used.

```powershell
metadata -add "title:$(date, MM/DD/YY) $(series)"
metadata -add "title:$(date, 'MMMM DD YYYY')"
```

If `date:2017-01-02` exists, `$(date, MM/DD/YY)` becomes `01/02/17`. Tab after
`$` or `$(` completes tag namespaces from the current results; after `$(date,`
it completes formats.

## Transform Syntax

Transforms are function calls. The preferred form is bare, Python-style:

```text
name(arg1, arg2, ...)
```

The angle-bracket form `<name(arg1, arg2, ...)>` is still accepted for
compatibility with older templates. You can nest transforms and mix both
forms, e.g. `if($(season), s$(season)e$(ep), e$(ep))`.

Transforms run **after** `$(namespace)` placeholders are expanded. `#(` still
works inside them.

When typing a tag value, the completer offers every transform with its
signature and help text, like an IDE. Start typing a function name (for
example `tri`) and it suggests `trim($value) — Strip whitespace`. Completion
also stays open inside the call, suggesting `$(namespace)` for value arguments
and date formats for format arguments, until `)` closes the call.

### Padding

Use `padding(width, value)` (aliases `pad`, `zfill`) to zero-pad a value.
Note the width comes first.

```powershell
metadata -add "code:epadding(00, $(episode))"
metadata -add "code:epadding(2, $(episode))"
```

If `episode:3` exists, both resolve to:

```text
code:e03
```

Width can be written as `00` (width 2), `000` (width 3), or a plain integer.

### Default

Use `default(value, fallback)` when a namespace may be missing.

```powershell
metadata -add "season:default($(season), 0)"
metadata -add "disc:default($(disc), 1)"
```

If `season:` is missing, the first resolves to `season:0`.

### Replace

Use `replace(value, old, new)` for substring replacement.

```powershell
metadata -add "slug:replace($(title), ' ', _)"
metadata -add "slug:replace($(series), -, _)"
```

Quote a space when you want to replace literal spaces — bare spaces are trimmed
by argument parsing, so `' '` is the reliable form.

### Regex

Use `regex(value, pattern, replacement)` (alias `sub`) for regex substitution.

```powershell
metadata -add "channel:regex($(channel), '^kabbalah ', '')"
metadata -add "slug:regex($(title), '[^a-z0-9]+', '_')"
metadata -add "year:regex($(date), '(\d{4})', '\1')"
```

Notes:

- The pattern and replacement are Python `re` syntax, so capture groups
  `(...)` and backreferences `\1` / `\g<name>` are supported.
- Quote arguments that contain spaces, commas, or parentheses so they are not
  split. Use `'...'` (single quotes) around the pattern and replacement; a
  literal backslash stays literal inside single quotes.
- If the pattern does not compile, or any placeholder is unresolved, the whole
  templated tag is skipped.

### Increment

Use `increment(value, amount)` for integer offsets. The second argument is
optional and defaults to `1`. Aliases: `inc`, `add`.

```powershell
metadata -add "episode_next:increment($(episode), 1)"
metadata -add "disc_next:increment($(disc), -1)"
```

If `episode:3` exists, the first resolves to `episode_next:4`.

### Date Transform

Use `date(value, format)` (aliases `formatdate`, `datefmt`). If you omit the
format, **Preferences → Date Format** is used.

```powershell
metadata -add "title:date($(date), MM/DD/YYYY) $(series)"
```

### If / If2

Same idea as Mp3tag `$if` / `$if2` and Picard `$if` / `$if2`.

```powershell
metadata -add "code:if($(season), spadding(00, $(season))epadding(00, $(episode)), epadding(00, $(episode)))"
metadata -add "album:if2($(album), $(series), unknown)"
```

- `if(cond, then, else)` uses `then` when `cond` is non-empty; `else` is optional.
- `if2(a, b, ...)` (alias `coalesce`) returns the first non-empty argument.

### Trim, Left, Right

```powershell
metadata -add "title:trim($(title))"
metadata -add "prefix:left($(title), 20)"
metadata -add "suffix:right($(title), 8)"
metadata -add "rest:cutleft($(title), 3)"
```

`cutleft` / `cutright` drop that many characters from the start or end.

### Slug And Case

```powershell
metadata -add "slug:slug($(title))"
metadata -add "name:caps($(creator))"
metadata -add "code:upper($(code))"
```

`slug` / `sanitize` lowercases and turns non-alphanumerics into `_`.
`caps` / `titlecase` title-cases words. Prefer keeping source tags lowercase
and using these only for cleanup.

## Commas Inside Transforms

Tag arguments still support comma-separated tags, but commas inside transform
calls are preserved.

```powershell
metadata -add "code:epadding(00, $(episode)),title:$(series)"
```

This stays as two tags, not three fragments.

## Extracting From Titles

`-extract` derives structured tags from a title using a pattern. `(name)`
captures a namespaced tag, `(name:part)` labels a date part, `#` matches a
number and is not stored, and `#?` matches an optional number.

Bare `(date)` matches a date token at that position (`10-22-16`, `10/22/16`,
`2016-10-22`, `20161022`, or spaced `10 22 16`) and writes `date:` using
**Preferences → Date Format**. Unmatched text after the template is ignored,
so you do not need to capture the rest of the title.

Use `[a|b]` in the literal text for local alternatives (optional spaces
around each choice). Use `|` between whole patterns for larger ORs:

```powershell
metadata -add -extract "part (episode)[-|:](name)"
metadata -add -extract "part (episode): (name)|part (episode) - (name)"
```

`[-|:]` matches a dash or a colon, so both `part 1: serpents...` and
`part 3 - satan...` fill `episode:` and `name:`.

```powershell
metadata -add -extract "(date)"
metadata -add -extract "(date) kabbalah"
metadata -add -extract "(date:month)-(date:day)-(date:year)"
```

Date parts are `year` (`yy`), `month` (`mm`), and `day` (`dd`). 2-digit years
become 20xx.

### Title A — dated lecture

```text
10-22-16 Kabbalah For Heretics Broadcast on The Gnostic Gospels w Rebbe Yakov-Leib HaKohain
```

```powershell
metadata -add -extract "(date) (series) broadcast on the (topic) w (creator)"
```

derives:

```text
date:2016-10-22
series:kabbalah for heretics
topic:gnostic gospels
creator:rebbe yakov-leib hakohain
```

### Title B — numbered track

```text
06 - 6 1st stage separation
```

```powershell
metadata -add -extract "(track) - (title)"
```

derives:

```text
track:06
title:6 1st stage separation
```

### Title C — course with a part

```text
ancient greek intensive course - part 9
```

```powershell
metadata -add -extract "(series) - part (track)"
```

derives:

```text
series:ancient greek intensive course
track:9
```

## Worked Examples

Each title below shows a small pipeline: extract structure, then build new tags
from it with transforms.

### Title A: dated lecture

Starting tags:

```text
title:10-22-16 Kabbalah For Heretics Broadcast on The Gnostic Gospels w Rebbe Yakov-Leib HaKohain
```

```powershell
# pull out the date and speaker
metadata -add -extract "(date) (series) broadcast on the (topic) w (creator)"

# a clean, sortable lecture code
metadata -add "code:$(series)-$(date, YYYYMMDD)"

# a slug for filenames
metadata -add "slug:slug($(series))-$(date, YYYYMMDD)"

# a human-friendly display title
metadata -add "title:$(date, 'MMM D, YYYY') — $(series): $(topic) w $(creator)"
```

Results:

```text
date:2016-10-22
series:kabbalah for heretics
topic:gnostic gospels
creator:rebbe yakov-leib hakohain
code:kabbalah for heretics-20161022
slug:kabbalah_for_heretics-20161022
title:Oct 22, 2016 — kabbalah for heretics: gnostic gospels w rebbe yakov-leib hakohain
```

### Title B: numbered track

Starting tags:

```text
title:06 - 6 1st stage separation
album:wave iii
artist:monroe institute
```

```powershell
# split the leading number off the title
metadata -add -extract "(track) - (title)"

# normalise the track number to two digits
metadata -add "track:padding(2, $(track))"

# build a stable sort key
metadata -add "sort:$(album)-padding(2, $(track))"
```

Results:

```text
track:06
title:6 1st stage separation
sort:wave iii-06
```

### Title C: course with a part

Starting tags:

```text
title:ancient greek intensive course - part 9
```

```powershell
# extract the course name and part number
metadata -add -extract "(series) - part (track)"

# rebuild a canonical title from the extracted tags
metadata -add "title:$(track) - $(series)"

# a numeric "next part" helper
metadata -add "part_next:increment($(track), 1)"
```

Results:

```text
series:ancient greek intensive course
track:9
title:9 - ancient greek intensive course
part_next:10
```

## Missing Values

If a placeholder or transform cannot be resolved, the whole templated tag is
skipped instead of being written literally.

Skipped cases:

- `title:$(missing_namespace)` when no such tag exists
- `code:padding(x, $(episode))` when the padding width is invalid
- `name:regex($(title), '[')` when the pattern does not compile

The command logs a warning summary for skipped unresolved templates.

## Recommended Patterns

Episode-style numbering:

```powershell
metadata -add "code:epadding(00, $(episode))"
```

Title synthesis from extracted tags:

```powershell
metadata -add -extract "(series) - part (track)" "title:$(track) - $(series)"
```

Delete a derived title tag:

```powershell
metadata -delete "title:$(track) - $(series)"
```

Reuse an existing value under a new namespace:

```powershell
metadata -add "album:$(series)"
```

## Mass Tagging Recipes

Patterns most useful when cleaning or normalizing large existing tag sets.

### Build A Stable Episode Code

```powershell
metadata -add "code:epadding(00, $(episode))"
```

```text
episode:3   -> code:e03
episode:12  -> code:e12
```

### Build Season-Episode Labels

```powershell
metadata -add "code:spadding(00, $(season))epadding(00, $(episode))"
```

With `season:1` and `episode:3`, this becomes `code:s01e03`.

### Fill Missing Season Values First

```powershell
metadata -add "season:default($(season), 0)" "code:spadding(00, $(season))epadding(00, $(episode))"
```

This keeps the later code template predictable when source metadata is
incomplete.

### Promote Existing Values Into New Namespaces

```powershell
metadata -add "album:$(series)"
metadata -add "label:$(publisher)"
metadata -add "subtitle:$(title)"
```

### Create URL-Safe Slugs

```powershell
metadata -add "slug:slug($(title))"
metadata -add "slug:replace($(title), ' ', _)"
```

`slug` lowercases and turns non-alphanumerics into `_`. For one-off edits,
`replace` is enough.

### Create Offset Tags

```powershell
metadata -add "episode_next:increment($(episode), 1)"
metadata -add "episode_prev:increment($(episode), -1)"
```

### Delete A Derived Tag Predictably

Once a tag was created from a template, remove it with the same template:

```powershell
metadata -delete "title:$(track) - $(series)"
metadata -delete "code:spadding(00, $(season))epadding(00, $(episode))"
```

This is safer than typing the fully expanded value during bulk cleanup.

### Keep Inputs Lowercase Upstream

The cleanest workflows normalize source tags before using them in templates:

- keep namespace names lowercase
- keep values lowercase when you create/import them
- use templates to compose values; `lower` / `caps` are for cleanup, not the
  default path

```text
series:ancient greek intensive course
episode:3
publisher:oxford
```

## Current Supported Syntax Summary

- `$(namespace)` inserts an existing tag value (`#(` still works)
- `$(namespace, 1)` takes the 1st value when several exist (`-1` is last)
- `$(date, MM/DD/YY)` formats a date value
- `$(track #)` trailing `#` in the namespace is ignored
- Transforms use function-call syntax: `name(args)` (or `<name(args)>` for compatibility)
- `padding` / `pad` / `zfill` — `padding(width, value)`
- `default(value, fallback)` fallback when missing
- `replace(value, old, new)` substring replace
- `regex(value, pattern, replacement)` / `sub` regex replace
- `increment(value, n)` / `inc` integer offset (default `1`)
- `date(value, format)` / `formatdate` date format (Preferences if omitted)
- `if(cond, then, else)` / `if2(a, b, ...)` (Mp3tag/Picard style)
- `trim` / `left` / `right` / `cutleft` / `cutright`
- `slug` / `sanitize` filename-safe slug
- `lower` / `upper` / `caps` case (optional cleanup)

New transforms should follow the same function-call style. Keep the argument
order in `TAG_FUNCTIONS` (in `cmdlet/_tag_utils.py`) in sync with the evaluator
so completion help stays accurate.
