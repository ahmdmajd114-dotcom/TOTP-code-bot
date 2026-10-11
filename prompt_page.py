"""Public copyable prompt page for the MedBox website."""

from html import escape


MEDICAL_INFOGRAPHIC_PROMPT = r"""Create a high-resolution, comprehensive medical study infographic
in the style of a professional, densely organized medical lecture
summary poster designed for medical students and examination revision.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
TOPIC AND CONTENT
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

MAIN TOPIC: [INSERT MEDICAL TOPIC]

LECTURE TITLE: [INSERT LECTURE TITLE]

TARGET AUDIENCE:
Medical students preparing for clinical examinations, written exams,
OSCEs, and medical licensing examinations.

SOURCE MATERIAL:
[INSERT LECTURE NOTES, TEXTBOOK CONTENT, OR SPECIFIC SUBTOPICS.
If no source material is provided, use established medical knowledge
and prioritize high-yield concepts.]

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
1. OVERALL VISUAL STYLE
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Reproduce the visual language of a premium medical lecture revision
sheet with the following characteristics:

- Clean white or very light blue background.
- Dense but carefully organized educational content.
- Deep navy-blue and royal-blue section headers.
- Rounded rectangular panels with thin colored borders.
- Pastel color coding using light blue, mint green, pale pink,
  lavender, and soft yellow.
- Professional sans-serif typography.
- Bold section headings and clearly emphasized medical terminology.
- Small, scientifically accurate anatomical illustrations.
- Simplified clinical diagrams and medical illustrations.
- Neat tables, flowcharts, classification trees, and diagnostic algorithms.
- Thin connecting arrows and clearly defined relationships.
- Consistent spacing, alignment, and typography throughout.
- Minimal decorative elements; prioritize information density
  and academic readability.

The final result should resemble a professionally designed medical
school lecture summary prepared using PowerPoint or a medical
infographic design tool.

Avoid childish illustrations, excessive decoration, exaggerated
cartoon characters, photographic backgrounds, and unnecessary
visual effects.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
2. PAGE LAYOUT
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Use a landscape-oriented canvas with an aspect ratio of 5:4
or another landscape ratio that accommodates dense medical content.

Place a prominent dark-blue title banner across the top.

Below the title, organize the information into a precise,
multi-column grid of numbered educational sections.

Use approximately 9–12 sections when the topic is sufficiently broad.
For narrower topics, use fewer sections rather than adding filler.

Each section must have:
- A numbered blue header.
- A concise, informative title.
- A light pastel-colored content area.
- Clear internal spacing and alignment.

Arrange the sections in a logical learning sequence:
overview → anatomy → definitions → classification →
clinical presentation → causes → investigations →
diagnosis → management → complications → examination pearls.

Adapt the sequence to the selected medical topic.

Make the sections visually distinct while maintaining a unified
design system across the entire page.

Ensure that all content remains within the canvas boundaries.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
3. CONTENT ORGANIZATION
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Select the most relevant categories for the requested topic.

Possible sections include:

1. Overview and Definition
2. Relevant Anatomy and Physiology
3. Classification
4. Important Definitions
5. Etiology and Risk Factors
6. Pathophysiology
7. Clinical Presentation
8. Differential Diagnosis
9. Investigations
10. Diagnostic Approach or Algorithm
11. Management and Treatment
12. Complications and High-Yield Exam Points

Do not force every category into every infographic.
Select the most useful sections for the particular topic.

Use concise bullet points rather than paragraphs.

Include comparison tables when two or more conditions
need to be differentiated.

Use flowcharts to explain diagnostic pathways,
clinical decision-making, and disease progression.

Use classification trees to explain disease subtypes.

Use small anatomical illustrations when they improve understanding.

Highlight important diagnostic criteria, distinguishing features,
first-line investigations, emergency interventions,
and common examination traps.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
4. MEDICAL ILLUSTRATIONS
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Include small, accurate, educational medical illustrations
where appropriate.

Examples:
- Anatomy topics: labeled anatomical structures.
- Cardiovascular topics: heart diagrams, ECGs, and circulation.
- Respiratory topics: lungs, airways, and respiratory pathology.
- Gastrointestinal topics: digestive organs and anatomical cross-sections.
- Renal topics: kidneys, glomeruli, and urinalysis findings.
- Neurology topics: brain structures, neural pathways, and examination signs.
- Hematology topics: blood cells and peripheral blood smears.
- Infectious diseases: relevant microorganisms and clinical manifestations.
- Surgery topics: anatomical diagrams, operative concepts, and algorithms.
- Pediatrics topics: age-appropriate clinical illustrations.

Every illustration must directly support the educational content.

Avoid irrelevant illustrations or images that occupy space
without adding medical information.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
5. TABLES AND DIAGNOSTIC ALGORITHMS
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Create compact, highly readable tables for important comparisons.

Examples include:
- Disease A versus Disease B.
- Central versus peripheral causes.
- Structural versus functional disorders.
- Screening versus confirmatory investigations.
- First-line versus second-line treatments.

For diagnostic algorithms:
- Start with the presenting symptom or clinical problem.
- Use clearly labeled decision points.
- Connect decisions with directional arrows.
- Label branches with concise answers such as YES and NO.
- End each branch with the appropriate diagnosis,
  investigation, or next clinical action.

Ensure that arrows connect to the correct boxes
and that the clinical logic is medically sound.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
6. COLOR CODING AND TYPOGRAPHY
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Use a consistent color system:

- Dark navy blue: main title and primary section headers.
- Royal blue: secondary headings and important labels.
- Light blue: anatomy, investigations, and diagnostic pathways.
- Mint green: definitions, clinical features, and key concepts.
- Pale pink: important warnings, differential diagnoses,
  and critical clinical findings.
- Soft yellow: classifications, comparisons, and examination pearls.
- Light lavender: supplementary information.

Use bold text selectively to emphasize high-yield terminology.

Ensure strong contrast between text and background.

Keep headings larger than body text.

Use compact but readable text without excessive compression.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
7. MEDICAL ACCURACY
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

All medical information must be scientifically accurate,
clinically relevant, and appropriate for medical students.

Use accepted medical terminology.

Preserve important distinctions between similar diseases.

Do not invent diagnostic criteria, laboratory values,
drug doses, treatment recommendations, or clinical facts.

If source material is provided, prioritize its content and
preserve the intended meaning.

If information is uncertain or controversial, do not present
it as an established fact.

Do not omit essential safety information from emergency topics.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
8. TEXT QUALITY AND READABILITY
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Use English medical terminology and English explanations
unless another language is explicitly requested.

Spell all medical terms correctly.

Use concise, high-yield bullet points.

Avoid long paragraphs, repeated information, unnecessary sentences,
gibberish, and misspelled terminology.

Do not invent additional lecture sections unrelated to the topic.

IMPORTANT:
Information density must never compromise readability.

If the complete topic cannot fit legibly on one page,
prioritize the most important concepts or divide the content
into multiple coordinated infographic pages.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
9. FINAL OUTPUT
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Produce a polished, high-resolution medical lecture summary infographic.

The finished page should feature:
- A prominent title banner.
- Numbered blue section headers.
- Multiple neatly aligned educational panels.
- Concise medical bullet points.
- Relevant anatomical illustrations.
- Compact comparison tables.
- Diagnostic flowcharts where appropriate.
- Consistent pastel color coding.
- A professional academic appearance.

The final result must look like a carefully designed,
high-yield medical lecture revision sheet suitable for
medical students, examination preparation, and lecture notes.

The layout should be information-rich, organized, precise,
and visually consistent from top to bottom.

Create original content and illustrations specific to the requested topic."""


def build_prompt_page_html() -> str:
    prompt = escape(MEDICAL_INFOGRAPHIC_PROMPT)
    return f"""<!doctype html>
<html lang="en" dir="ltr">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <meta name="theme-color" content="#126f73">
  <title>Medical Infographic Prompt | MedBox Pro</title>
  <style>
    :root{{--teal:#126f73;--dark:#0c5559;--navy:#132d3a;--cream:#f8f5ee;--line:#d9e5df}}
    *{{box-sizing:border-box}} body{{margin:0;color:var(--navy);background:linear-gradient(145deg,#f9f6ef,#edf5f2);font-family:Inter,Arial,sans-serif}}
    header{{padding:24px;background:linear-gradient(120deg,var(--dark),var(--teal));color:#fff}}
    .head,.main{{width:min(980px,calc(100% - 28px));margin:auto}} .head{{display:flex;align-items:center;justify-content:space-between;gap:20px}}
    h1{{margin:0;font-size:clamp(22px,4vw,34px)}} .brand{{font-weight:800;white-space:nowrap}} .main{{padding:28px 0 50px}}
    .hint{{line-height:1.7;color:#52696e}} .toolbar{{position:sticky;top:12px;z-index:2;display:flex;justify-content:flex-end;margin:16px 0}}
    button{{border:0;border-radius:12px;padding:12px 20px;background:var(--teal);color:#fff;font:inherit;font-weight:700;cursor:pointer;box-shadow:0 8px 20px #126f7330}}
    button:hover{{background:var(--dark)}} button.done{{background:#27855f}}
    pre{{margin:0;padding:24px;overflow:auto;white-space:pre-wrap;word-break:break-word;border:1px solid var(--line);border-radius:20px;background:#fff;box-shadow:0 16px 38px #163c4110;font:14px/1.65 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}}
    footer{{padding:0 16px 30px;text-align:center;color:#617478}} footer a{{color:var(--dark);font-weight:700}}
    @media(max-width:560px){{header{{padding:20px 14px}}.head{{align-items:flex-start;flex-direction:column}}pre{{padding:17px;font-size:12px}}}}
  </style>
</head>
<body>
  <header><div class="head"><div><h1>Medical Infographic Prompt</h1><div>Ready to copy and customize</div></div><div class="brand">MedBox Pro</div></div></header>
  <main class="main">
    <p class="hint">Replace the bracketed fields with your medical topic, lecture title, and source material, then copy the complete prompt.</p>
    <div class="toolbar"><button id="copy" type="button">Copy full prompt</button></div>
    <pre id="prompt">{prompt}</pre>
  </main>
  <footer><a href="/">Back to MedBox catalogue</a></footer>
  <script>
    const button=document.getElementById('copy');
    button.addEventListener('click',async()=>{{
      await navigator.clipboard.writeText(document.getElementById('prompt').textContent);
      button.textContent='Copied ✓';button.classList.add('done');
      setTimeout(()=>{{button.textContent='Copy full prompt';button.classList.remove('done')}},1800);
    }});
  </script>
</body></html>"""
