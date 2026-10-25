"""Where each resume check comes from, and what the problem costs the job seeker.

Every finding of the Canadian check (app/modules/resumes/canadian.py) and the
ATS check (app/modules/resumes/ats.py) has a `Basis` here, shown under it:
- `impact`: what happens because of it -- worded only as strongly as its source;
- `kind`: OFFICIAL (a Canadian government or human-rights body), VENDOR (an
  applicant tracking system maker's own documentation), RESEARCH (a published
  study), DIRECT (seen in the file itself) or PRACTICE (common advice with no official rule behind it --
  said so on the page, never dressed up as a rule);
- `quote`: the source's words, copied from the page (checked October 2026), or
  `summary` when the page couldn't be read word for word and is paraphrased.

Don't add a finding without a basis, and don't strengthen an `impact` beyond
what its source says: the page promises these are real.
"""
from __future__ import annotations

from dataclasses import dataclass

OFFICIAL, VENDOR, RESEARCH, DIRECT, PRACTICE = "official", "vendor", "research", "direct", "practice"
KIND_LABELS = {
    OFFICIAL: "Official guidance",
    VENDOR: "ATS maker's documentation",
    RESEARCH: "Published research",
    DIRECT: "Found in your file",
    PRACTICE: "Common advice, not an official rule",
}


@dataclass(frozen=True)
class Source:
    name: str
    url: str


@dataclass(frozen=True)
class Basis:
    impact: str
    kind: str
    source: Source | None = None
    quote: str | None = None
    summary: str | None = None

    @property
    def label(self) -> str:
        return KIND_LABELS[self.kind]


JOB_BANK = Source("Job Bank (Government of Canada): How to write a good resume",
                  "https://www.jobbank.gc.ca/findajob/resources/write-good-resume")
OHRC = Source("Ontario Human Rights Commission: Designing application forms",
              "https://www.ohrc.on.ca/en/iv-human-rights-issues-all-stages-employment/4-designing-application-forms")
PORTAL_DATES = Source("Language Portal of Canada: Date: How to write the date correctly",
                      "https://our-languages.canada.ca/en/writing-tips-plus/date-how-to-write-the-date-correctly")
PORTAL_SPELLING = Source("Language Portal of Canada: Why Canadian spelling is different",
                         "https://our-languages.canada.ca/en/blogue-blog/canadian-spelling-eng")
IRCC_EMPLOYERS = Source("IRCC: The Employer's Roadmap to hiring internationally trained workers",
                        "https://www.canada.ca/en/immigration-refugees-citizenship/corporate/publications-manuals/"
                        "employer-roadmap-hiring-retaining-internationally-trained-workers.html")
GREENHOUSE = Source("Greenhouse (an applicant tracking system) support: Unsuccessful resume parse",
                    "https://support.greenhouse.io/hc/en-us/articles/200989175-Unsuccessful-resume-parse")
HBS = Source("Harvard Business School: How to tap the talent automated HR platforms miss",
             "https://www.library.hbs.edu/working-knowledge/how-to-tap-the-talent-automated-hr-platforms-miss")

_GREENHOUSE_FORMATTING = ("Resume formatting issues: Resumes that include graphics, photos, or word art; Complex "
                          "resumes with tables, headers, and footers; Resumes that have a columned layout; Resumes "
                          "that are uploaded as an image, rather than a document")
_MISREAD = ("Applicant tracking systems read your resume into fields (name, jobs, dates). When they misread it, "
            "those fields come out wrong or empty, and the recruiter's search can miss you.")

BASIS: dict[str, Basis] = {
    # ---- Canadian conventions (canadian.py) ----
    "photo": Basis(
        "Job Bank says a photo can lower your chances of getting the job, and human-rights guidance tells "
        "Canadian employers not to ask for one.", OFFICIAL, JOB_BANK,
        quote="Although in some countries it may be acceptable to include a photo, it is not the norm in Canada. "
              "It can actually lower your chances of obtaining a position and divert the whole focus of your "
              "resume."),
    "personal": Basis(
        "Canadian employers are told not to ask for these before a job offer, so a resume that volunteers them "
        "looks out of place. Job Bank advises leaving them out.", OFFICIAL, OHRC,
        quote="Employers should not ask for the date of birth or a birth certificate, or for other documents "
              "that indicate age … No questions about marital status are permitted."),
    "declaration": Basis(
        "Canadian resumes don't have one, so it takes space your experience could use.", PRACTICE),
    "references": Basis(
        "Job Bank says to give references only when an employer asks. Listing them hands your referees' "
        "details to every employer you apply to.", OFFICIAL, JOB_BANK,
        quote="Keep references on a separate sheet and provide them only when they are specifically requested."),
    "high_school": Basis(
        "Once you have a degree or diploma, high school takes space without telling employers anything new.",
        PRACTICE),
    "dates": Basis(
        "An employer can't tell whether 05/04/2019 means April or May, so your work history can read wrong.",
        OFFICIAL, PORTAL_DATES,
        summary="All-numeric dates are ambiguous; the Government of Canada's guidance is to write them as "
                "YYYY-MM-DD, or to write the month as a word."),
    "spelling": Basis(
        "A small signal, not a deal-breaker: consistent Canadian spelling reads as local.", OFFICIAL,
        PORTAL_SPELLING,
        summary="Canadians generally use -our and -re (colour, centre), and most favour -ize (organize). The "
                "Canadian Oxford lists -ise too, so consistency matters most."),
    "length": Basis(
        "Job Bank recommends two pages at most; recruiters reading a long resume may not reach your best work.",
        OFFICIAL, JOB_BANK,
        quote="Limit your resume to two pages … Older jobs and experience that are more than 15 years old should "
              "either be cut out or minimized."),
    "location": Basis(
        "Without a city and province, an employer can't tell whether you're nearby or would need to move.",
        OFFICIAL, JOB_BANK, quote="Your resume should list your name, address, email and phone number."),
    "phone": Basis(
        "An international number can be harder or costlier for an employer to call.", PRACTICE),
    "credentials": Basis(
        "Employers may not know how your education compares. An assessment answers that, and the Government of "
        "Canada tells employers they can ask for one.", OFFICIAL, IRCC_EMPLOYERS,
        quote="You can ask potential employees to have their credentials assessed as part of their job "
              "applications."),
    "objective": Basis(
        "An objective says what you want; a summary tells the employer what you offer, which is what they're "
        "screening for.", PRACTICE),

    # ---- ATS readability (ats.py) ----
    "ats_tables": Basis(_MISREAD, VENDOR, GREENHOUSE, quote=_GREENHOUSE_FORMATTING),
    "ats_columns": Basis(_MISREAD + " Columns are often read across both at once, mixing them.", VENDOR, GREENHOUSE,
                         quote=_GREENHOUSE_FORMATTING),
    "ats_header": Basis(
        "Some parsers skip the page header and footer, so your contact details can be lost.", VENDOR, GREENHOUSE,
        quote=_GREENHOUSE_FORMATTING),
    "ats_text_boxes": Basis(
        "Text boxes sit outside the normal flow of the page, and parsers can skip them or read them out of "
        "order.", PRACTICE),
    "ats_graphics": Basis(
        "Icons, skill bars and pictures carry no text a parser can read, and Greenhouse lists graphics as a "
        "cause of failed parsing.", VENDOR, GREENHOUSE, quote=_GREENHOUSE_FORMATTING),
    "ats_file_size": Basis(
        "Greenhouse can't read resumes over 2.5 MB at all; the recruiter gets the file but none of it fills "
        "your profile.", VENDOR, GREENHOUSE,
        quote="Resumes over 2.5MB can't be parsed."),
    "ats_unreadable": Basis(
        "We tried to copy the text out of your PDF and got garbled characters. Any software reading it gets "
        "the same.", DIRECT),
    "ats_email": Basis(
        "We couldn't find an email address in your resume's text. If it's in a picture or the page header, "
        "software can miss it too, and an employer can't reply.", DIRECT, JOB_BANK,
        quote="Your resume should list your name, address, email and phone number."),
    "ats_phone": Basis(
        "We couldn't find a phone number in your resume's text.", DIRECT, JOB_BANK,
        quote="Your resume should list your name, address, email and phone number."),
    "ats_headings": Basis(
        "Parsers sort your resume by its headings. Unusual ones (\"My journey\") can leave your jobs out of "
        "the experience fields.", PRACTICE),
}

# The rule each basis backs, for the page's "Where these checks come from" list.
RULES: dict[str, str] = {
    "photo": "No photo", "personal": "No personal details (birth date, marital status, religion…)",
    "references": "References only when asked", "dates": "Dates written with the month as a word",
    "spelling": "Canadian spelling", "length": "Two pages at most", "location": "City and province at the top",
    "credentials": "Explain education from outside Canada", "declaration": "No declaration",
    "high_school": "No high school once you have a degree", "phone": "A Canadian phone number",
    "objective": "A summary rather than an objective",
    "ats_tables": "ATS: no tables", "ats_columns": "ATS: one column", "ats_header": "ATS: contact details not in "
    "the page header", "ats_graphics": "ATS: important information as text, not pictures",
    "ats_file_size": "ATS: file under 2.5 MB", "ats_text_boxes": "ATS: no text boxes",
    "ats_unreadable": "ATS: text that copies out of the PDF", "ats_email": "Email address as text",
    "ats_phone": "Phone number as text", "ats_headings": "ATS: standard section headings",
}

# Shown once above the ATS findings: why being read correctly matters at all.
ATS_CONTEXT = Basis(
    "Employers that use this software mostly rely on it for the first cut, before a person reads your "
    "application.", RESEARCH, HBS,
    quote="more than 90 percent of employers we surveyed that used an RMS relied on it to make a first cut")


def basis(key: str) -> Basis | None:
    return BASIS.get(key)
