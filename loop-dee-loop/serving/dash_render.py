"""Pure DASH MPD fragment rendering: SegmentTimeline lines, video Representations, the audio AdaptationSet."""

from __future__ import annotations

from fractions import Fraction



def _dash_frame_rate(frame_rate: float) -> str:
    """DASH ``@frameRate`` is ``N`` or ``N/D`` -- decimals like ``25.000`` are schema-invalid."""
    frac = Fraction(frame_rate).limit_denominator(1001)
    return str(frac.numerator) if frac.denominator == 1 else f"{frac.numerator}/{frac.denominator}"


def _join_events(events: list[tuple[int, str]]) -> str:
    """ISO 23009-1 / MediaTailor ("first Event in each Period") both assume an
    EventStream's Events are in presentationTime order."""
    return "\n".join(xml for _, xml in sorted(events, key=lambda e: e[0]))


def render_segment_timeline(items: list[tuple], compact: bool) -> str:
    """The lines inside a <SegmentTimeline>. `items` mixes ("c", text) XML
    comments and ("s", t, d) segments, in order. `full` writes every segment
    with @t and @d. `compact` omits @t where a segment starts exactly where the
    previous one ended and folds runs of equal @d into @r; a comment ends a run
    (so it stays right before the segment it describes), and the first segment
    always keeps its @t (a front-trimmed window must not change anyone's t)."""
    out: list[str] = []
    if not compact:
        for item in items:
            out.append(f"        <!-- {item[1]} -->" if item[0] == "c" else f'        <S t="{item[1]}" d="{item[2]}" />')
        return "\n".join(out)
    run: list[int] | None = None  # [t, d, repeats, write_t]
    expected = None  # where the next segment starts if contiguous

    def flush() -> None:
        nonlocal run
        if run is not None:
            t, d, r, write_t = run
            out.append(
                "        <S"
                + (f' t="{t}"' if write_t else "")
                + f' d="{d}"'
                + (f' r="{r}"' if r else "")
                + " />"
            )
            run = None

    for item in items:
        if item[0] == "c":
            flush()
            out.append(f"        <!-- {item[1]} -->")
            continue
        _, t, d = item
        if run is not None and t == expected and d == run[1]:
            run[2] += 1
        else:
            flush()
            run = [t, d, 0, expected is None or t != expected]
        expected = t + d
    flush()
    return "\n".join(out)


def render_video_representations(
    reps: list[dict], timescale: int, start_number_attr: str, compact: bool
) -> str:
    """The <Representation> elements of the video AdaptationSet. Each item of
    `reps` has the rendition's `name`, `variant` (bandwidth/codecs/size/rate),
    `media` and `init_attr` (built with the rendition name in the path),
    `timeline` (rendered <S> lines) and `pto_attr`. Each
    Representation is named after its rendition. `full` gives every one its own
    SegmentTemplate/SegmentTimeline. `compact`, when every rendition
    shares the same timeline, offset and URL shape, writes one SegmentTemplate
    on the AdaptationSet addressing them through $RepresentationID$."""

    def attrs(v: dict) -> str:
        return (
            f'bandwidth="{v["bandwidth"]}" codecs="{v["codecs"]}" width="{v["width"]}" '
            f'height="{v["height"]}" frameRate="{_dash_frame_rate(v["frame_rate"])}"'
        )

    def template(r: dict, media: str, init_attr: str, indent: str) -> str:
        return (
            f'{indent}<SegmentTemplate media="{media}"{init_attr}\n'
            f'{indent}                 timescale="{timescale}"{start_number_attr}{r["pto_attr"]}>\n'
            f"{indent}  <SegmentTimeline>\n{r['timeline']}\n{indent}  </SegmentTimeline>\n"
            f"{indent}</SegmentTemplate>"
        )

    if compact:
        shaped = [
            (
                r["timeline"],
                r["pto_attr"],
                r["media"].replace(r["name"], "$RepresentationID$"),
                r["init_attr"].replace(r["name"], "$RepresentationID$"),
            )
            for r in reps
        ]
        if len(set(shaped)) == 1:
            _, _, media, init_attr = shaped[0]
            body = "\n".join(f'      <Representation id="{r["name"]}" {attrs(r["variant"])} />' for r in reps)
            return template(reps[0], media, init_attr, "      ") + "\n" + body
    out = []
    for r in reps:
        out.append(
            f'      <Representation id="{r["name"]}" {attrs(r["variant"])}>\n'
            + template(r, r["media"], r["init_attr"], "        ")
            + "\n      </Representation>"
        )
    return "\n".join(out)


def render_audio_adaptation_set(
    a: dict, media: str, init: str, timeline: str, timescale: int, start_number_attr: str,
    pto_attr: str, compact: bool,
) -> str:
    """The audio <AdaptationSet> (a single Representation, id `audio` like its
    URL path). `full` keeps the SegmentTemplate inside the Representation;
    `compact` moves it up to the AdaptationSet, addressed via $RepresentationID$."""
    tpl_attrs = f'timescale="{timescale}"{start_number_attr}{pto_attr}'
    rep_attrs = f'id="audio" bandwidth="{a["bandwidth"]}" codecs="{a["codecs"]}"'
    if compact:
        media, init = (x.replace("audio/", "$RepresentationID$/", 1) for x in (media, init))
        return f'''
    <AdaptationSet mimeType="audio/mp4" segmentAlignment="true" startWithSAP="1">
      <SegmentTemplate media="{media}" initialization="{init}"
                       {tpl_attrs}>
        <SegmentTimeline>
{timeline}
        </SegmentTimeline>
      </SegmentTemplate>
      <Representation {rep_attrs} />
    </AdaptationSet>'''
    return f'''
    <AdaptationSet mimeType="audio/mp4" segmentAlignment="true" startWithSAP="1">
      <Representation {rep_attrs}>
        <SegmentTemplate media="{media}" initialization="{init}"
                         {tpl_attrs}>
          <SegmentTimeline>
{timeline}
          </SegmentTimeline>
        </SegmentTemplate>
      </Representation>
    </AdaptationSet>'''
