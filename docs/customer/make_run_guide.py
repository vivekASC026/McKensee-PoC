r"""Builds the customer run guide for pattern analysis (SDM Pattern Analysis - Run Guide.docx).

    .venv\Scripts\python.exe docs\customer\make_run_guide.py
"""

from docx import Document
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Pt, RGBColor, Cm

OUT = str(__import__("pathlib").Path(__file__).with_name("SDM Pattern Analysis - Run Guide.docx"))

doc = Document()
for s in doc.sections:
    s.left_margin = s.right_margin = Cm(2.2)
    s.top_margin = s.bottom_margin = Cm(2)

st = doc.styles["Normal"]
st.font.name = "Calibri"
st.font.size = Pt(11)
st.element.rPr.rFonts.set(qn("w:eastAsia"), "Calibri")
for name, size in (("Heading 1", 15), ("Heading 2", 12.5)):
    h = doc.styles[name]
    h.font.name = "Calibri"
    h.font.size = Pt(size)
    h.font.color.rgb = RGBColor(0x1F, 0x3A, 0x5F)

NAVY = "1F3A5F"


def shade(cell, color):
    tc = cell._tc.get_or_add_tcPr()
    sh = OxmlElement("w:shd")
    sh.set(qn("w:val"), "clear")
    sh.set(qn("w:color"), "auto")
    sh.set(qn("w:fill"), color)
    tc.append(sh)


def rich(par, text):
    """**bold** and `code` inline."""
    import re
    for part in re.split(r"(\*\*[^*]+\*\*|`[^`]+`)", text):
        if not part:
            continue
        if part.startswith("**"):
            par.add_run(part[2:-2]).bold = True
        elif part.startswith("`"):
            r = par.add_run(part[1:-1])
            r.font.name = "Consolas"
            r.font.size = Pt(10)
        else:
            par.add_run(part)
    return par


def p(text, style=None):
    return rich(doc.add_paragraph(style=style), text)


def bullets(items):
    for i in items:
        p(i, "List Bullet")


def steps(items):
    # Numbered by hand: Word's own list numbering would run on from one list to the next.
    for n, i in enumerate(items, 1):
        para = doc.add_paragraph()
        para.paragraph_format.left_indent = Cm(0.9)
        para.paragraph_format.first_line_indent = Cm(-0.6)
        para.paragraph_format.space_after = Pt(4)
        lines = i.split("\n")
        rich(para, f"{n}.\t" + lines[0])
        for extra in lines[1:]:
            para.add_run().add_break()
            rich(para, extra)


def code(text):
    para = doc.add_paragraph()
    para.paragraph_format.left_indent = Cm(0.8)
    r = para.add_run(text)
    r.font.name = "Consolas"
    r.font.size = Pt(10.5)
    r.bold = True


def note(text, color="FFF4E5"):
    t = doc.add_table(rows=1, cols=1)
    t.alignment = WD_TABLE_ALIGNMENT.CENTER
    c = t.rows[0].cells[0]
    shade(c, color)
    rich(c.paragraphs[0], text)
    doc.add_paragraph()


def table(header, rows, widths=None):
    t = doc.add_table(rows=1, cols=len(header))
    t.style = "Table Grid"
    for i, h in enumerate(header):
        c = t.rows[0].cells[i]
        shade(c, NAVY)
        r = c.paragraphs[0].add_run(h)
        r.bold = True
        r.font.color.rgb = RGBColor(0xFF, 0xFF, 0xFF)
    for row in rows:
        cells = t.add_row().cells
        for i, v in enumerate(row):
            rich(cells[i].paragraphs[0], v)
    if widths:
        for row in t.rows:
            for i, w in enumerate(widths):
                row.cells[i].width = Cm(w)
    doc.add_paragraph()


# ---------------------------------------------------------------- title
t = doc.add_paragraph()
t.alignment = WD_ALIGN_PARAGRAPH.LEFT
r = t.add_run("SDM Pattern Analysis")
r.bold = True
r.font.size = Pt(24)
r.font.color.rgb = RGBColor(0x1F, 0x3A, 0x5F)
sub = doc.add_paragraph()
r = sub.add_run("Run guide: starting the tool and finding, reviewing, saving and exporting patterns")
r.font.size = Pt(12)
r.italic = True

p("This guide covers **pattern analysis only**. SDM is installed and its settings "
  "file (`.env`) is in place, and the FACETS data has been loaded into the SDM "
  "tables. Other screens of the tool (Connect, Discover and Profile, Map and Review, "
  "Generation) are not set up in this deployment and are not used.")

# ---------------------------------------------------------------- 1
doc.add_heading("1. Start the tool", level=1)
steps([
    "Open a **Command Prompt** (Start menu, type `cmd`).",
    "Go to the SDM folder, for example: `cd /d C:\\SDM`",
    "Run:",
])
code("scripts\\ui.cmd")
p("The window prints that the tool has started, and the browser opens at "
  "**http://127.0.0.1:8777**. If no browser opens, open Chrome or Edge and type that address.")
note("**Keep the Command Prompt window open** while you use the tool: closing it stops "
     "the tool. To stop it, click in that window and press **Ctrl+C** (or close the window).")
p("Each person runs the tool on their own machine; the address works only on that "
  "machine. Everyone reads the same SDM tables, so patterns saved by one person are "
  "seen by all.")

# ---------------------------------------------------------------- 2
doc.add_heading("2. Find your way around", level=1)
p("The first time, the tool opens on the **Connect** screen; after that it opens where you "
  "left off. **Do not press \"Read the database\"** on Connect: it profiles every table of "
  "the database, which is a long, heavy read and not needed for patterns.")
table(["Where", "What it is", "Use it?"], [
    ["Left menu, top: **Workspace**", "**Database** or **Files**: which kind of source every step shows", "Leave on **Database**"],
    ["Left menu, middle", "The steps: Connect, Discover and Profile, Map and Review, **Patterns**, Generation", "Only **Patterns**"],
    ["Left menu, lower: **Settings**", "**Pattern Settings**, Generation Rules", "**Pattern Settings** (once, see section 3)"],
    ["Patterns, top left", "**Members / Claims** switch", "Yes: chooses what you analyze"],
    ["Bottom left", "The database and server the tool is pointed at; Dark / Light", "Information only"],
], widths=[4.2, 8.5, 4.3])
p("Click **Patterns** on the left. Five tabs appear across the top of it:")
table(["Tab", "What it answers"], [
    ["**Targeted Analysis**", "\"What kinds of members (or claims) are in this group or category?\" The main screen; section 4."],
    ["**Full Book Scan**", "\"What are the largest kinds of members across the whole book?\" Section 8."],
    ["**Custom Pattern**", "\"How many members look like this description I have in mind?\" Section 7."],
    ["Requirement Match", "Not covered in this guide."],
    ["**Saved Patterns**", "Every saved pattern, from any tab, ready to download. Section 8."],
], widths=[4.2, 12.8])

# ---------------------------------------------------------------- 3
doc.add_heading("3. Choose what patterns are found on (Pattern Settings)", level=1)
p("Do this once, and again only when you want patterns to differ on other things. "
  "The choice is stored in the SDM tables, so it applies for everyone.")
steps([
    "Click **Pattern Settings** under **Settings** on the left menu.",
    "There are two columns, **Members** and **Claims**, each with groups of features: "
    "**Plan and group**, **Provider and network**, and **Member and claim** "
    "(for example age, sex, marital status, relationship, tenure, claim type, claim status, "
    "in or out of network, PCP). Features marked **number** are ranges, such as age.",
    "Under **Fields Used to Find Patterns**, check the fields you want. Patterns differ by "
    "the checked fields and nothing else: check age and sex, and the patterns are groups of "
    "members alike in age and sex.",
    "Click **Save** under that column (Members and Claims are saved separately). A green "
    "message confirms what patterns are now found on. **Defaults** puts back the starting choice.",
    "Click **Back to ...** at the top right (or any step on the left) to return.",
])
note("Fewer, meaningful fields give clearer patterns. Checking everything makes many "
     "small patterns that are hard to tell apart.", "EAF3FB")

doc.add_heading("Fields Counted in Patterns", level=2)
p("Below it, **Fields Counted in Patterns** chooses what **Counts and %** on a pattern, "
  "**Download every pattern's counts** and the download of saved patterns (section 9) count. "
  "This does not change how patterns are found, only what is counted about them.")
steps([
    "Under **Default fields**, uncheck any field you do not want counted (for example ZIP).",
    "Under **Added from the denorm**, pick any column of the member (or claims) denorm "
    "table in **Add a column…** and click **Add**. Its values are counted like the "
    "others: a date by its year, a number with many values in ten bands of about equal "
    "size. Columns with personal data (names, SSN, birth date) are not offered.",
    "Click **Save** under that column. **Defaults** puts back the starting fields.",
])
note("An added column is marked **counted only**: generated data does not follow it.",
     "EAF3FB")

# ---------------------------------------------------------------- 4
doc.add_heading("4. Targeted Analysis: find patterns", level=1)
steps([
    "At the top left of Patterns, choose **Members** or **Claims**. "
    "(Claims is grayed out if no claims were loaded.)",
    "On **Targeted Analysis**, the header shows the size of the book: Members, Groups and "
    "Plans (or Claims, Lines and Diagnoses for claims). The line under the heading says "
    "which fields patterns are found on, with a **Change these fields** link to Pattern Settings.",
    "Choose the scope:\n"
    "– **Group**: one employer group, or **Every group**.\n"
    "– **Product category** (members) or **Claim type** (claims): one, or every one.\n"
    "– **Number of patterns**: how many patterns to split the scope into. **Automatic** lets "
    "the tool pick the number that separates the data best; or choose 2 to 10.",
    "Click **Find Patterns**. A large scope (every group) takes longer than one group; "
    "the same scope a second time is quicker, because the result is kept.",
])
p("For claims, the screen also lists the **Most common diagnoses** and **Most common "
  "procedures** in the scope, with their descriptions and counts. These come from the "
  "loaded FACETS claims, not from a fixed list.")

doc.add_heading("Reading the results", level=2)
p("Two charts come first:")
bullets([
    "**Where each pattern sits**: each pattern's range (for example age) against "
    "**Everyone in scope**.",
    "**What sets each pattern apart**: for each pattern, the values that are more (or "
    "less) common in it than in the scope as a whole.",
])
p("Then one card per pattern. The heading of a card shows:")
table(["Part of the card", "Meaning"], [
    ["Name", "Made from what makes the pattern distinct, for example its most common values."],
    ["Size", "How many members (or claim lines) are in it."],
    ["Share bar", "Its share of the scope analyzed."],
    ["Band (very high, high, medium, low, rare)", "How large it is compared with the other patterns found across the book. **Rare** marks a small, unusual group, often the interesting edge cases for testing."],
], widths=[5.5, 11.5])
p("Under the heading, the facts:")
table(["Fact", "Meaning"], [
    ["Age 22–64, mean 41.3", "The youngest and oldest member in the pattern, and the average."],
    ["Tenure 3–120 months", "How long members have been enrolled."],
    ["Charge", "For claims: lowest and highest charged amount, and the average."],
    ["Places", "How many ZIP codes the members live in, and the most common city and state."],
    ["Sex F 62% M 38%", "The two most common values of a checked field and their shares in this pattern."],
    ["Marital M 88% E 5%", "Same: 88% of the pattern have marital status M, 5% have E. The codes are the FACETS codes as stored."],
], widths=[4.5, 12.5])

doc.add_heading("Opening a pattern", level=2)
p("Click a card's heading to open it. It shows the full mix of every feature (each "
  "value and its share) and, for claims, the top diagnoses. Of the buttons under it, "
  "use these:")
bullets([
    "**Counts and %**: every field of the pattern, every value, with how many and what "
    "share (below).",
    "**Show the source records**: the real FACETS records behind the pattern (section 5).",
    "**Save pattern**: keep it (section 6).",
])

doc.add_heading("Counts and %", level=2)
p("**Counts and %** lists, for the whole pattern, every value of every field with its "
  "count and its percentage, then every scenario as one row. Nothing is left out: a "
  "field with many values (diagnoses, ZIP codes) opens with **Show all**, and a "
  "scenario of a single member is listed like any other, since a rare scenario can be "
  "the important one. **Download every pattern's counts**, above the cards, writes the "
  "same for every pattern on screen, as Excel, PDF or CSV (section 9).")
p("What each field is, what it is counted in, and whether generation refers to it:")
table(["Field", "Counted in", "What it is", "Used in generation"], [
    ["Age band", "members", "Age on the reference date, in bands (0-17, 18-34, 35-49, "
     "50-64, 65+).", "Yes, referred into data generation."],
    ["Sex, Marital status, Relationship", "members", "As FACETS stores them, with what "
     "each code means (F Female; S Spouse ...).", "Yes, referred into data generation."],
    ["State, ZIP", "members", "The member's home address (needs the member address "
     "extract).", "Yes, referred into data generation (No, where the "
     "pattern's addresses are all missing)."],
    ["Group, Product category, Plan, Product", "members", "Where the member is enrolled, "
     "with names.", "Yes, referred into data generation."],
    ["Members with claims", "members", "How many of the pattern's members have any "
     "claim.", "No, not referred into and not generated."],
    ["Claim amount, Claim type, Diagnosis", "claims", "All claims of the pattern's "
     "members, counted together (never per member); amounts in dollar bands.", "No, not referred into and not generated."],
    ["Procedure", "claim lines", "All claim lines of the pattern's members.", "No, not referred into and not generated."],
    ["Claims patterns: claim type, status, network, amount, diagnosis, procedure",
     "claims (or claim lines)", "The claims in the pattern.", "Yes, referred into data generation."],
    ["Claims patterns: place of service, specialty", "claims", "The claims in the "
     "pattern.", "No, not referred into and not generated."],
    ["Claims patterns: member age band, sex", "claims", "The patients of the claims.", "No, not referred into and not generated."],
    ["Claims patterns: group, product category, plan, product", "claims", "Where the "
     "patients are enrolled; the claims go to the pattern's own members.", "Yes, referred into data generation."],
    ["Fields added in Pattern Settings", "members or claims", "Any column you chose to count.", "No, not referred into and not generated."],
    ["Scenario", "members (or claims)", "One combination of sex, age band and plan "
     "(claims: claim type and network) the pattern holds: its count and its % of the "
     "pattern.", "Yes, referred into data generation."],
], widths=[3.6, 2.4, 5.6, 5.4])
note("The **Used in generation** column has one of two answers. **Yes, referred into data "
     "generation**: generated data follows the pattern for that field, in the same "
     "shares. **No, not referred into and not generated**: the field is counted for "
     "information only. A pattern saved before October 15, 2026 keeps the older behavior "
     "(ages and amounts evenly between the lowest and highest; plans evenly) until it is "
     "saved again.")
p("**Generate a file**, **Generate into the database…** and **Scenarios** are for "
  "generating test data, which is not set up in this deployment. Do not use them.")

# ---------------------------------------------------------------- 5
doc.add_heading("5. The source records", level=1)
p("Shows the actual members (or claim lines) that make up the pattern, read from the "
  "loaded FACETS data, a page at a time (**Previous** / **Next**).")
bullets([
    "Names, dates of birth and identifiers are covered by default. The box "
    "**Show the N PHI columns** uncovers them: use it only where you are allowed to "
    "see member data, and not while sharing your screen.",
    "Selecting rows offers generation buttons; as above, generation is not used here.",
    "Click **Hide the source records** to close the list.",
])

# ---------------------------------------------------------------- 6
doc.add_heading("6. Save a pattern", level=1)
p("Open the pattern and click **Save pattern**. The button changes to **Saved**. "
  "Saving the same pattern twice keeps one copy.")
p("What is saved, in the SDM tables (so **every user sees it**):")
bullets([
    "How to find it again: the scope (group, product category or claim type), the number of patterns and which pattern it was.",
    "Its profile: sizes and shares, the mix of each feature, age and tenure ranges, top diagnoses and procedures, top locations.",
    "The list of its members (FACETS member keys).",
])
p("What is **not** saved: copies of the member or claim rows. When a saved pattern's rows "
  "are shown again, they are read from the latest loaded data.")
note("**After the next FACETS refresh**, a saved pattern's profile stays as it was on the "
     "day it was saved. Members who are no longer eligible drop out of the loaded data, "
     "so the rows shown for it can be fewer than its saved size.", "EAF3FB")

# ---------------------------------------------------------------- 7
doc.add_heading("7. Custom Pattern", level=1)
p("Describe a kind of member (or claim) yourself and see how many match.")
steps([
    "Open **Patterns > Custom Pattern**, and give it a **Pattern name**.",
    "In **Add a feature**, pick a feature (for example product category, sex, age). It "
    "appears with its values and how many members have each. Check the values you want; "
    "for a number such as age, set **From** and **To**. Add more features the same way.",
    "Click **Measure it** to see how many match and their profile, then **Save pattern** "
    "on the result to keep it.",
    "Optional: **Find Patterns Within** splits the members you described into "
    "patterns (with **Number of patterns** as in section 4).",
    "Optional, under **Variants**: choose **Vary by** (and **And by**) to make one "
    "pattern per value, for example the same description for each product category. "
    "**Make variants** measures them; save each with **Save**, or all with **Save all**.",
])

# ---------------------------------------------------------------- 8
doc.add_heading("8. Full Book Scan and Saved Patterns", level=1)
p("Instead of choosing a scope, the tool goes through every group (and, deeper, every "
  "product category, plan and product), finds the patterns in each, and ranks them by "
  "size. Useful to find kinds of members nobody thought to look for.")
steps([
    "Open **Patterns > Full Book Scan**.",
    "Choose **Depth**: **Groups**, **Groups and categories**, **Down to plans** or "
    "**Down to products**. Deeper means more combinations, and a longer first run.",
    "Click **Scan the Book**. The first scan at a depth takes several minutes on a "
    "large book; later ones at the same depth are quick until the next FACETS refresh.",
    "The table lists each pattern, **Where** it is (group › category › plan › product), "
    "its **Size** and **Share of book**. Click **Save** to save one.",
])
p("**Patterns > Saved Patterns** lists every saved pattern from any tab, with its "
  "type (members or claims) and **defined** for ones made under Custom Pattern.")
note("**Remove** deletes a saved pattern **for everyone**. Remove only patterns you saved yourself.")

# ---------------------------------------------------------------- 9
doc.add_heading("9. Download patterns as Excel, PDF or CSV", level=1)
p("From the screens (the tool running):")
bullets([
    "**One pattern**: open it, click **Counts and %**, then beside **Download as** click "
    "**Excel**, **PDF** or **CSV (plain text)**. The file holds exactly what the panel shows; "
    "the PDF is laid out like the panel, ready to print or send.",
    "**Saved patterns**: under **Patterns > Saved Patterns**, select the patterns you want, "
    "choose Excel, PDF or CSV under **Download as** and click **Download N selected**, or "
    "**Download all**.",
    "**Every pattern of an analysis**: **Download every pattern's counts as** above the "
    "cards: Excel, PDF or CSV.",
])
p("What is counted is chosen under **Pattern Settings**, **Fields Counted in Patterns** "
  "(section 3). Without the tool running, the same file for every saved pattern comes "
  "from a Command Prompt in the SDM folder:")
code("scripts\\customer\\export_patterns.cmd")
p("It writes every saved pattern to `exports\\patterns-<date>-<time>.xlsx` in the SDM "
  "folder and prints how many it wrote. To choose the file, name it after the command, "
  "for example `scripts\\customer\\export_patterns.cmd C:\\Temp\\patterns.xlsx`. "
  "It only reads the SDM tables and changes nothing.")
p("The workbook has **one sheet**. Every row is one value of one field of one pattern, "
  "one scenario of a pattern, or one tag. Use the filter on the header row to see one "
  "pattern, one field, or only the scenarios (Level = Scenario).")
table(["Column", "Contents"], [
    ["Domain", "Members or Claims."],
    ["Pattern, Pattern size, Counted in", "The pattern's name and how many members (or "
     "claims) it has."],
    ["Share of population", "The pattern's share of the scope it was found in."],
    ["Level", "**Pattern**: a value of a field across the whole pattern. **Scenario**: one "
     "scenario of the pattern, one row."],
    ["Field, Value, Description", "What is counted (Age band, Plan, Diagnosis ...), the "
     "value, and what the code means. For a scenario: the fields it combines and its "
     "values."],
    ["Count, Out of, %", "How many have this value, out of how many (members, claims or "
     "claim lines), and the percentage. For a scenario: its count and its % of the "
     "pattern."],
    ["Used in generation", "Yes, referred into data generation; or No, not referred "
     "into and not generated (section 4, Counts and %)."],
], widths=[4.6, 12.4])

# ---------------------------------------------------------------- 10
doc.add_heading("10. When something goes wrong", level=1)
table(["What you see", "What to do"], [
    ["The browser page is blank", "Press Ctrl+F5. If still blank, stop the tool (Ctrl+C) and start it again; report it to the SDM team if it persists."],
    ["\"This site can't be reached\"", "The tool is not running: start it (section 1) and keep its window open."],
    ["Claims is grayed out", "No claims are loaded in the SDM tables."],
    ["A red message after Find patterns", "Note the message and the scope chosen, and send both to the SDM team."],
    ["Results look out of date", "Patterns are measured on the data as of the last FACETS load. A new load is picked up automatically."],
], widths=[5.2, 11.8])

doc.save(OUT)
print(OUT)
