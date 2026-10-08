"""Skill vocabulary for everything that is not software.

`taxonomy.py` covers AI/ML and software. That was the right scope for one candidate
and the wrong scope for a product: a pharmacist's resume listing HPLC, GMP and
pharmacovigilance produced *zero* extracted skills, so the field detector fell back to
a title match and guessed "Quality Engineering".

Same discipline as the software taxonomy, for the same reason: a curated list can only
find what the candidate actually wrote. Asking a model to "extract the skills" from a
civil engineer's resume will return "project management" and "leadership" whether or
not they are on the page, and those become fabricated claims on a real application.

Aliases matter more than canonical names. A resume writes "Staad", "STAAD.Pro" and
"STAAD Pro"; all three must resolve to one fact or matching, deduplication and gap
analysis will disagree with each other.
"""

from __future__ import annotations

# canonical -> aliases (the canonical form is always matched too)
DOMAIN_SKILLS: dict[str, list[str]] = {
    # ─── Civil, structural, construction ────────────────────────────────────
    "autocad": ["auto cad", "auto-cad"],
    "staad pro": ["staad", "staad.pro", "staadpro"],
    "etabs": ["e-tabs"],
    "sap2000": ["sap 2000"],
    "revit": ["autodesk revit"],
    "primavera": ["primavera p6", "p6"],
    "ms project": ["microsoft project"],
    "surveying": ["total station", "land surveying"],
    "concrete technology": ["rcc", "reinforced concrete", "concrete mix design"],
    "structural analysis": ["structural design", "load analysis"],
    "estimation": ["boq", "bill of quantities", "quantity estimation"],
    "geotechnical": ["soil mechanics", "foundation design"],
    "site supervision": ["site management", "site execution"],
    "bim": ["building information modelling", "building information modeling"],
    # ─── Mechanical, manufacturing, automotive, aerospace ───────────────────
    "solidworks": ["solid works"],
    "catia": ["catia v5"],
    "creo": ["pro/engineer", "proe"],
    "ansys": ["ansys workbench"],
    "nx": ["siemens nx", "unigraphics"],
    "gd&t": ["geometric dimensioning", "gd and t"],
    "cnc": ["cnc machining", "cnc programming"],
    "hvac": ["heating ventilation"],
    "cfd": ["computational fluid dynamics"],
    "fea": ["finite element analysis"],
    "thermodynamics": [],
    "autosar": [],
    "canoe": ["vector canoe"],
    "vehicle dynamics": [],
    # ─── Quality, lean, process ─────────────────────────────────────────────
    "six sigma": ["lean six sigma", "green belt", "black belt"],
    "lean": ["lean manufacturing"],
    "kaizen": [],
    "5s": [],
    "iso 9001": ["iso9001"],
    "iso 27001": ["iso27001"],
    "fmea": ["failure mode effects analysis"],
    "spc": ["statistical process control"],
    "root cause analysis": ["rca", "8d", "5 why"],
    "calibration": [],
    # ─── Electrical, electronics, VLSI ──────────────────────────────────────
    "plc": ["programmable logic controller"],
    "scada": [],
    "verilog": ["system verilog", "systemverilog"],
    "vhdl": [],
    "cadence": ["cadence virtuoso"],
    "synopsys": ["design compiler"],
    "altium": ["altium designer"],
    "pcb design": ["pcb layout", "pcb"],
    "power systems": ["power distribution", "substation design"],
    "rtos": ["real time operating system", "freertos"],
    "microcontroller": ["mcu", "8051", "stm32"],
    "embedded c": ["embedded-c"],
    "arm": ["arm cortex"],
    # ─── Chemical, pharma, life sciences ────────────────────────────────────
    "hplc": ["high performance liquid chromatography"],
    "gc-ms": ["gcms", "gas chromatography"],
    "uv spectroscopy": ["uv-vis", "spectrophotometry"],
    "gmp": ["cgmp", "good manufacturing practice"],
    "glp": ["good laboratory practice"],
    "gcp": ["good clinical practice"],
    "regulatory affairs": ["regulatory", "anda", "dmf filing"],
    "pharmacovigilance": ["drug safety", "adverse event reporting"],
    "formulation": ["formulation development"],
    "method validation": ["analytical validation", "process validation", "validation"],
    "clinical trials": ["clinical research", "clinical trial management"],
    "aspen": ["aspen plus", "aspen hysys", "hysys"],
    "p&id": ["piping and instrumentation"],
    "hazop": ["hazard and operability"],
    "pcr": ["rt-pcr", "qpcr"],
    "cell culture": ["tissue culture"],
    "microbiology": ["microbiological testing"],
    "genomics": ["ngs", "next generation sequencing"],
    "bioinformatics": [],
    # ─── Healthcare and clinical ────────────────────────────────────────────
    "patient care": ["patient management"],
    "emr": ["electronic medical records", "ehr"],
    "phlebotomy": [],
    "icu": ["intensive care", "critical care"],
    "radiography": ["x-ray imaging", "radiology"],
    "physiotherapy": ["physical therapy"],
    # ─── Finance and accounting ─────────────────────────────────────────────
    "tally": ["tally erp", "tally prime"],
    "sap fico": ["sap fi", "sap co"],
    "quickbooks": ["quick books"],
    "gaap": ["us gaap"],
    "ifrs": ["ind as"],
    "financial modelling": ["financial modeling", "dcf", "valuation"],
    "gst": ["goods and services tax"],
    "tds": ["tax deducted at source"],
    "accounts payable": ["ap process"],
    "accounts receivable": ["ar process"],
    "bank reconciliation": ["brs"],
    "auditing": ["internal audit", "statutory audit"],
    "bloomberg": ["bloomberg terminal"],
    "equity research": [],
    # ─── Sales, marketing, CRM ──────────────────────────────────────────────
    "salesforce": ["sfdc"],
    "hubspot": [],
    "zoho crm": ["zoho"],
    "google analytics": ["ga4"],
    "google ads": ["adwords", "google adwords"],
    "meta ads": ["facebook ads", "instagram ads"],
    "seo": ["search engine optimization", "search engine optimisation"],
    "sem": ["search engine marketing"],
    "content marketing": [],
    "email marketing": ["mailchimp", "klaviyo"],
    "lead generation": ["lead gen", "prospecting"],
    "b2b sales": ["b2b"],
    "cold calling": [],
    "negotiation": [],
    # ─── HR and operations ──────────────────────────────────────────────────
    "recruitment": ["talent acquisition", "sourcing candidates", "hiring"],
    "onboarding": [],
    "payroll": ["payroll processing"],
    "hrms": ["hris", "workday hcm", "successfactors"],
    "employee engagement": [],
    "performance management": ["appraisal"],
    "sap mm": ["sap materials management"],
    "inventory management": ["stock management"],
    "vendor management": ["supplier management"],
    "procurement": ["purchasing", "sourcing"],
    "logistics": ["freight", "shipping", "transportation"],
    "demand planning": ["demand forecasting"],
    "sop": ["standard operating procedure"],
    # ─── Design and content ─────────────────────────────────────────────────
    "figma": [],
    "adobe xd": ["xd"],
    "sketch": [],
    "photoshop": ["adobe photoshop", "ps"],
    "illustrator": ["adobe illustrator"],
    "after effects": ["adobe after effects"],
    "premiere pro": ["adobe premiere"],
    "indesign": ["adobe indesign"],
    "canva": [],
    "wireframing": ["wireframe"],
    "prototyping": ["prototype design"],
    "user research": ["usability testing", "ux research"],
    "copywriting": ["copy writing"],
    "technical writing": ["documentation writing"],
    "wordpress": [],
    # ─── Legal ──────────────────────────────────────────────────────────────
    "contract drafting": ["contract management", "contract review"],
    "litigation": [],
    "due diligence": [],
    "compliance": ["regulatory compliance"],
    "company secretary": ["cs qualified"],
    # ─── Education ──────────────────────────────────────────────────────────
    "curriculum": ["curriculum development", "curriculum design"],
    "lesson planning": [],
    "e-learning": ["elearning", "online teaching"],
    "instructional design": [],
    # ─── Hospitality and services ───────────────────────────────────────────
    "guest relations": ["guest services"],
    "food safety": ["haccp", "fssai"],
    "facility management": ["facilities management"],
    "pos": ["point of sale"],
    # ─── Software the original taxonomy missed ──────────────────────────────
    # Found by the test asserting every job-family skill is a canonical taxonomy name:
    # families listed these, no resume could ever match them, and the gap was silent.
    "kotlin": [],
    "swift": ["swiftui"],
    "flutter": [],
    "dart": [],
    "react native": ["react-native"],
    "html": ["html5"],
    "css": ["css3", "scss", "sass", "tailwind"],
    "spring": ["spring boot", "springboot"],
    "jenkins": [],
    "pytest": [],
    "looker": ["looker studio"],
    "deep learning": ["dl"],
    "selenium": ["selenium webdriver"],
    "statistics": ["statistical analysis", "hypothesis testing"],
    "ci/cd": ["ci cd", "continuous integration"],
    "cnn": ["convolutional neural network"],
    # ─── Cross-sector tools ─────────────────────────────────────────────────
    "ms office": ["microsoft office", "msoffice"],
    "erp": ["enterprise resource planning"],
    "sap": ["sap erp"],
    "oracle": ["oracle erp"],
    "jira": [],
    "confluence": [],
    "agile": ["agile methodology", "scrum"],
    "pmp": ["project management professional"],
    "stakeholder management": [],
    "process improvement": [],
}

# Terms too short or too overloaded to match on their own. "nx" appears inside
# hundreds of words, "pos" is a part of speech, "ap" and "ar" are everywhere.
DOMAIN_AMBIGUOUS = frozenset(
    {
        "nx",
        "pos",
        "sap",
        "erp",
        "lean",
        "5s",
        "arm",
        "gst",
        "tds",
        "cs qualified",
        "p6",
        # Short or ordinary-English names that collide constantly in prose.
        "dl",
        "css",
        "html",
        "dart",
        "swift",
        "spring",
        "statistics",
    }
)
