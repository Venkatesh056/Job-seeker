"""Job families across every sector, not just software.

The system was built around eight AI/ML resume archetypes. That was right for one
candidate and wrong as a product: a civil engineer's resume would be scored against
"PyTorch, Transformers, RAG" and rejected by its own relevance gate.

A **family** is one hiring lane — "Data Analytics", "Structural Engineering",
"Financial Accounting". Each carries three different vocabularies, and the distinction
matters because each is consumed by a different part of the system:

  * `queries` — what to *ask a job board for*. Phrases a recruiter puts in a title.
  * `title_markers` — what to *recognise in a title we got back*. Substrings, matched
    longest-first so "machine learning engineer" beats "engineer".
  * `skills` — what to *look for in a resume*, for detecting the candidate's field.

`related` is the "and then more related jobs" the user asked for: picking Data Science
should offer Data Analytics and ML Engineering without dragging in Civil Engineering.
It is a hand-curated adjacency list rather than an embedding similarity, because the
adjacency a job seeker cares about is about *hiring lanes*, not about word vectors —
"Data Analyst" and "Business Analyst" are adjacent in the market whatever their
descriptions say.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Family:
    id: str
    label: str
    group: str
    queries: tuple[str, ...]
    title_markers: tuple[str, ...]
    skills: tuple[str, ...] = ()
    related: tuple[str, ...] = ()
    # The resume archetype to tailor with. Families outside software fall back to
    # "custom", which reorders by what the job asked for and invents no emphasis.
    archetype: str = "custom"


def _f(
    id_: str,
    label: str,
    group: str,
    queries: tuple[str, ...],
    title_markers: tuple[str, ...],
    skills: tuple[str, ...] = (),
    related: tuple[str, ...] = (),
    archetype: str = "custom",
) -> Family:
    return Family(id_, label, group, queries, title_markers, skills, related, archetype)


# ─── Technology ─────────────────────────────────────────────────────────────

TECHNOLOGY: tuple[Family, ...] = (
    _f(
        "ai_engineering",
        "AI Engineering",
        "Technology",
        ("AI Engineer", "Applied AI Engineer", "AI Developer", "AI Software Engineer"),
        ("ai engineer", "applied ai", "ai developer", "ai software"),
        ("pytorch", "tensorflow", "llm", "transformers", "python", "fastapi", "docker"),
        ("machine_learning", "genai_agentic", "nlp", "data_science", "mlops"),
        archetype="ai_engineer",
    ),
    _f(
        "machine_learning",
        "Machine Learning",
        "Technology",
        ("Machine Learning Engineer", "ML Engineer", "Machine Learning Scientist"),
        ("machine learning engineer", "ml engineer", "machine learning scientist", "ml scientist"),
        ("pytorch", "tensorflow", "scikit-learn", "xgboost", "python", "mlflow"),
        ("ai_engineering", "data_science", "mlops", "deep_learning", "computer_vision"),
        archetype="ml_engineer",
    ),
    _f(
        "deep_learning",
        "Deep Learning",
        "Technology",
        ("Deep Learning Engineer", "Neural Network Engineer", "Deep Learning Researcher"),
        ("deep learning", "neural network"),
        ("pytorch", "tensorflow", "keras", "cnn", "jax"),
        ("machine_learning", "computer_vision", "nlp", "ai_research"),
        archetype="ml_engineer",
    ),
    _f(
        "genai_agentic",
        "GenAI / Agentic AI / RAG",
        "Technology",
        (
            "Generative AI Engineer",
            "GenAI Engineer",
            "LLM Engineer",
            "AI Agent Engineer",
            "RAG Engineer",
            "Prompt Engineer",
        ),
        (
            "generative ai",
            "genai",
            "llm engineer",
            "ai agent",
            "agentic",
            "rag ",
            "prompt engineer",
        ),
        ("llm", "rag", "langchain", "langgraph", "ai agents", "prompt engineering", "llamaindex"),
        ("ai_engineering", "nlp", "machine_learning"),
        archetype="genai_engineer",
    ),
    _f(
        "nlp",
        "NLP & Speech",
        "Technology",
        (
            "NLP Engineer",
            "Natural Language Processing Engineer",
            "Speech Recognition Engineer",
            "Conversational AI Engineer",
        ),
        ("nlp", "natural language", "speech", "conversational ai", "asr", "linguist"),
        ("nlp", "transformers", "bert", "spacy", "whisper", "hugging face", "speech recognition"),
        ("genai_agentic", "machine_learning", "ai_engineering"),
        archetype="nlp_engineer",
    ),
    _f(
        "computer_vision",
        "Computer Vision",
        "Technology",
        ("Computer Vision Engineer", "Image Processing Engineer", "Perception Engineer"),
        ("computer vision", "image processing", "perception engineer", "cv engineer"),
        ("opencv", "computer vision", "yolo", "cnn", "pytorch"),
        ("machine_learning", "deep_learning", "robotics"),
        archetype="ml_engineer",
    ),
    _f(
        "data_science",
        "Data Science",
        "Technology",
        ("Data Scientist", "Applied Scientist", "Research Scientist", "Decision Scientist"),
        ("data scientist", "applied scientist", "research scientist", "decision scientist"),
        ("python", "sql", "pandas", "scikit-learn", "statistics", "r"),
        ("machine_learning", "data_analytics", "data_engineering", "ai_engineering"),
        archetype="data_scientist",
    ),
    _f(
        "data_analytics",
        "Data Analytics",
        "Technology",
        (
            "Data Analyst",
            "Business Intelligence Analyst",
            "BI Developer",
            "Analytics Engineer",
            "Reporting Analyst",
        ),
        (
            "data analyst",
            "bi analyst",
            "business intelligence",
            "analytics engineer",
            "reporting analyst",
            "bi developer",
        ),
        ("sql", "excel", "power bi", "tableau", "dax", "pandas", "looker"),
        ("data_science", "business_analysis", "data_engineering"),
        archetype="data_analyst",
    ),
    _f(
        "data_engineering",
        "Data Engineering",
        "Technology",
        ("Data Engineer", "ETL Developer", "Big Data Engineer", "Data Platform Engineer"),
        ("data engineer", "etl developer", "big data", "data platform"),
        ("spark", "airflow", "sql", "python", "kafka", "snowflake", "dbt", "etl"),
        ("data_analytics", "data_science", "cloud_devops", "backend"),
        archetype="ml_engineer",
    ),
    _f(
        "mlops",
        "MLOps & ML Platform",
        "Technology",
        ("MLOps Engineer", "ML Platform Engineer", "ML Infrastructure Engineer"),
        ("mlops", "ml platform", "ml infrastructure", "ml ops"),
        ("mlflow", "kubernetes", "docker", "airflow", "aws", "terraform"),
        ("machine_learning", "cloud_devops", "data_engineering"),
        archetype="ml_engineer",
    ),
    _f(
        "cloud_devops",
        "Cloud & DevOps",
        "Technology",
        (
            "Cloud Engineer",
            "DevOps Engineer",
            "Site Reliability Engineer",
            "Platform Engineer",
            "Cloud Architect",
            "Infrastructure Engineer",
        ),
        (
            "cloud engineer",
            "devops",
            "site reliability",
            "sre",
            "platform engineer",
            "cloud architect",
            "infrastructure engineer",
        ),
        ("aws", "azure", "gcp", "kubernetes", "docker", "terraform", "jenkins", "ci/cd"),
        ("backend", "mlops", "cybersecurity", "software_engineering"),
        archetype="software_engineer",
    ),
    _f(
        "software_engineering",
        "Software Engineering",
        "Technology",
        ("Software Engineer", "Software Developer", "SDE", "Application Developer"),
        ("software engineer", "software developer", "sde", "application developer", "programmer"),
        ("python", "java", "javascript", "git", "sql", "c++"),
        ("backend", "frontend", "fullstack", "cloud_devops", "mobile"),
        archetype="software_engineer",
    ),
    _f(
        "backend",
        "Backend Engineering",
        "Technology",
        (
            "Backend Engineer",
            "Backend Developer",
            "API Developer",
            "Java Developer",
            "Python Developer",
            "Node.js Developer",
        ),
        (
            "backend",
            "back-end",
            "api developer",
            "java developer",
            "python developer",
            "node developer",
            "golang developer",
            ".net developer",
        ),
        ("java", "python", "node.js", "fastapi", "django", "spring", "postgresql", "redis"),
        ("software_engineering", "fullstack", "cloud_devops", "data_engineering"),
        archetype="software_engineer",
    ),
    _f(
        "frontend",
        "Frontend Engineering",
        "Technology",
        (
            "Frontend Engineer",
            "Frontend Developer",
            "React Developer",
            "UI Developer",
            "Angular Developer",
        ),
        ("frontend", "front-end", "react developer", "ui developer", "angular", "vue developer"),
        ("javascript", "typescript", "react", "html", "css", "next.js"),
        ("fullstack", "software_engineering", "mobile", "ux_design"),
        archetype="software_engineer",
    ),
    _f(
        "fullstack",
        "Full Stack Engineering",
        "Technology",
        ("Full Stack Developer", "Full Stack Engineer", "MERN Developer", "MEAN Developer"),
        ("full stack", "fullstack", "full-stack", "mern", "mean"),
        ("javascript", "react", "node.js", "python", "mongodb", "sql"),
        ("frontend", "backend", "software_engineering"),
        archetype="software_engineer",
    ),
    _f(
        "mobile",
        "Mobile Engineering",
        "Technology",
        (
            "Android Developer",
            "iOS Developer",
            "Mobile Application Developer",
            "Flutter Developer",
            "React Native Developer",
        ),
        (
            "android",
            "ios developer",
            "mobile developer",
            "mobile application",
            "flutter",
            "react native",
            "swift developer",
            "kotlin developer",
        ),
        ("kotlin", "swift", "flutter", "react native", "java", "dart"),
        ("frontend", "software_engineering", "fullstack"),
        archetype="software_engineer",
    ),
    _f(
        "cybersecurity",
        "Cybersecurity",
        "Technology",
        (
            "Security Engineer",
            "Cybersecurity Analyst",
            "Information Security Analyst",
            "Penetration Tester",
            "SOC Analyst",
        ),
        (
            "security engineer",
            "cybersecurity",
            "information security",
            "penetration test",
            "soc analyst",
            "infosec",
            "security analyst",
        ),
        ("linux", "python", "iso 27001"),
        ("cloud_devops", "network_engineering", "software_engineering"),
    ),
    _f(
        "qa_testing",
        "QA & Test Engineering",
        "Technology",
        (
            "QA Engineer",
            "Test Engineer",
            "Automation Test Engineer",
            "SDET",
            "Quality Assurance Analyst",
        ),
        (
            "qa engineer",
            "test engineer",
            "sdet",
            "quality assurance",
            "automation test",
            "test analyst",
        ),
        ("selenium", "pytest", "jira"),
        ("software_engineering", "backend"),
        archetype="software_engineer",
    ),
    _f(
        "network_engineering",
        "Network Engineering",
        "Technology",
        (
            "Network Engineer",
            "Network Administrator",
            "Systems Administrator",
            "Network Security Engineer",
        ),
        (
            "network engineer",
            "network administrator",
            "system administrator",
            "sysadmin",
            "network security",
        ),
        ("linux",),
        ("cybersecurity", "cloud_devops", "it_support"),
    ),
    _f(
        "it_support",
        "IT Support & Operations",
        "Technology",
        (
            "IT Support Engineer",
            "Technical Support Engineer",
            "Desktop Support",
            "Service Desk Analyst",
            "IT Operations",
        ),
        (
            "it support",
            "technical support",
            "desktop support",
            "service desk",
            "helpdesk",
            "it operations",
        ),
        ("linux", "ms office"),
        ("network_engineering", "cloud_devops"),
    ),
    _f(
        "embedded",
        "Embedded & Firmware",
        "Technology",
        (
            "Embedded Software Engineer",
            "Firmware Engineer",
            "Embedded Systems Engineer",
            "IoT Engineer",
        ),
        ("embedded", "firmware", "iot engineer", "rtos"),
        ("c++", "rtos", "microcontroller", "embedded c"),
        ("electronics_engineering", "software_engineering", "robotics"),
    ),
    _f(
        "ai_research",
        "AI Research",
        "Technology",
        ("Research Engineer", "AI Researcher", "Research Intern", "Machine Learning Researcher"),
        ("ai researcher", "research engineer", "machine learning researcher", "research intern"),
        ("pytorch", "deep learning"),
        ("deep_learning", "machine_learning", "nlp"),
        archetype="ai_engineer",
    ),
    _f(
        "robotics",
        "Robotics & Automation",
        "Technology",
        ("Robotics Engineer", "Automation Engineer", "Controls Engineer"),
        ("robotics", "automation engineer", "controls engineer", "ros "),
        ("python", "c++", "plc", "scada"),
        ("embedded", "computer_vision", "mechanical_engineering"),
    ),
)

# ─── Core engineering (non-software) ────────────────────────────────────────

ENGINEERING: tuple[Family, ...] = (
    _f(
        "civil_engineering",
        "Civil Engineering",
        "Engineering",
        (
            "Civil Engineer",
            "Site Engineer",
            "Structural Engineer",
            "Construction Engineer",
            "Project Engineer Civil",
        ),
        (
            "civil engineer",
            "site engineer",
            "structural engineer",
            "construction engineer",
            "geotechnical",
        ),
        (
            "autocad",
            "staad pro",
            "revit",
            "surveying",
            "concrete technology",
            "structural analysis",
            "primavera",
            "estimation",
            "geotechnical",
            "site supervision",
            "bim",
        ),
        ("structural_engineering", "construction_management", "mechanical_engineering"),
    ),
    _f(
        "structural_engineering",
        "Structural Engineering",
        "Engineering",
        ("Structural Engineer", "Structural Design Engineer", "Bridge Engineer"),
        ("structural engineer", "structural design", "bridge engineer"),
        ("staad pro", "etabs", "sap2000", "autocad", "structural analysis"),
        ("civil_engineering", "construction_management"),
    ),
    _f(
        "construction_management",
        "Construction & Project Management",
        "Engineering",
        (
            "Construction Manager",
            "Project Manager Construction",
            "Site Supervisor",
            "Quantity Surveyor",
            "Planning Engineer",
        ),
        (
            "construction manager",
            "site supervisor",
            "quantity surveyor",
            "planning engineer",
            "project engineer",
        ),
        ("primavera", "ms project", "estimation", "site supervision"),
        ("civil_engineering", "structural_engineering", "operations"),
    ),
    _f(
        "mechanical_engineering",
        "Mechanical Engineering",
        "Engineering",
        (
            "Mechanical Engineer",
            "Design Engineer",
            "Mechanical Design Engineer",
            "Maintenance Engineer",
        ),
        (
            "mechanical engineer",
            "mechanical design",
            "design engineer",
            "maintenance engineer",
            "hvac engineer",
        ),
        (
            "solidworks",
            "autocad",
            "catia",
            "ansys",
            "creo",
            "gd&t",
            "thermodynamics",
            "fea",
            "cnc",
            "hvac",
        ),
        ("manufacturing_engineering", "automotive", "robotics", "civil_engineering"),
    ),
    _f(
        "electrical_engineering",
        "Electrical Engineering",
        "Engineering",
        ("Electrical Engineer", "Power Systems Engineer", "Electrical Design Engineer"),
        ("electrical engineer", "power systems", "electrical design", "substation"),
        ("autocad", "plc", "scada", "power systems", "matlab"),
        ("electronics_engineering", "manufacturing_engineering", "robotics"),
    ),
    _f(
        "electronics_engineering",
        "Electronics & VLSI",
        "Engineering",
        (
            "Electronics Engineer",
            "VLSI Engineer",
            "Hardware Engineer",
            "PCB Design Engineer",
            "Verification Engineer",
        ),
        (
            "electronics engineer",
            "vlsi",
            "hardware engineer",
            "pcb design",
            "rtl design",
            "verification engineer",
            "asic",
        ),
        ("verilog", "vhdl", "cadence", "altium", "pcb design"),
        ("embedded", "electrical_engineering", "semiconductor"),
    ),
    _f(
        "semiconductor",
        "Semiconductor & Chip Design",
        "Engineering",
        (
            "Physical Design Engineer",
            "DFT Engineer",
            "Analog Design Engineer",
            "Semiconductor Process Engineer",
        ),
        ("physical design", "dft engineer", "analog design", "semiconductor", "silicon"),
        ("verilog", "cadence", "synopsys"),
        ("electronics_engineering", "embedded"),
    ),
    _f(
        "chemical_engineering",
        "Chemical & Process Engineering",
        "Engineering",
        ("Chemical Engineer", "Process Engineer", "Production Engineer Chemical"),
        ("chemical engineer", "process engineer", "petrochemical", "refinery"),
        ("aspen", "p&id", "hazop", "method validation"),
        ("manufacturing_engineering", "quality_engineering", "pharma"),
    ),
    _f(
        "manufacturing_engineering",
        "Manufacturing & Production",
        "Engineering",
        (
            "Production Engineer",
            "Manufacturing Engineer",
            "Plant Engineer",
            "Industrial Engineer",
            "Process Improvement Engineer",
        ),
        (
            "production engineer",
            "manufacturing engineer",
            "plant engineer",
            "industrial engineer",
            "shop floor",
            "production supervisor",
        ),
        ("six sigma", "kaizen", "cnc", "spc", "process improvement"),
        ("mechanical_engineering", "quality_engineering", "supply_chain", "operations"),
    ),
    _f(
        "quality_engineering",
        "Quality Engineering",
        "Engineering",
        (
            "Quality Engineer",
            "QA/QC Engineer",
            "Quality Control Inspector",
            "Quality Assurance Engineer",
        ),
        (
            "quality engineer",
            "qa/qc",
            "quality control",
            "quality inspector",
            "quality assurance engineer",
        ),
        ("iso 9001", "six sigma", "spc", "fmea", "root cause analysis", "calibration"),
        ("manufacturing_engineering", "mechanical_engineering", "operations"),
    ),
    _f(
        "automotive",
        "Automotive Engineering",
        "Engineering",
        ("Automotive Engineer", "Vehicle Engineer", "Powertrain Engineer", "ADAS Engineer"),
        ("automotive", "vehicle engineer", "powertrain", "adas", "autosar"),
        ("catia", "autosar", "canoe", "vehicle dynamics", "matlab"),
        ("mechanical_engineering", "embedded", "manufacturing_engineering"),
    ),
    _f(
        "aerospace",
        "Aerospace Engineering",
        "Engineering",
        ("Aerospace Engineer", "Aeronautical Engineer", "Avionics Engineer"),
        ("aerospace", "aeronautical", "avionics", "aircraft"),
        ("catia", "ansys", "cfd", "fea"),
        ("mechanical_engineering", "embedded"),
    ),
)

# ─── Business and operations ────────────────────────────────────────────────

BUSINESS: tuple[Family, ...] = (
    _f(
        "business_analysis",
        "Business Analysis",
        "Business",
        (
            "Business Analyst",
            "Functional Consultant",
            "Product Analyst",
            "Business Systems Analyst",
        ),
        ("business analyst", "functional consultant", "product analyst", "systems analyst"),
        ("sql", "excel", "jira", "stakeholder management", "power bi"),
        ("data_analytics", "product_management", "consulting", "operations"),
    ),
    _f(
        "product_management",
        "Product Management",
        "Business",
        (
            "Product Manager",
            "Associate Product Manager",
            "Technical Product Manager",
            "Product Owner",
        ),
        ("product manager", "product owner", "apm", "technical product"),
        ("agile", "user research", "jira", "sql"),
        ("business_analysis", "ux_design", "project_management"),
    ),
    _f(
        "project_management",
        "Project & Program Management",
        "Business",
        ("Project Manager", "Program Manager", "Scrum Master", "Delivery Manager"),
        ("project manager", "program manager", "scrum master", "delivery manager", "pmo"),
        ("agile", "jira", "ms project", "pmp", "stakeholder management", "confluence"),
        ("product_management", "operations", "consulting", "construction_management"),
    ),
    _f(
        "finance_accounting",
        "Finance & Accounting",
        "Business",
        (
            "Financial Analyst",
            "Accountant",
            "Finance Manager",
            "Accounts Executive",
            "Audit Associate",
            "Chartered Accountant",
        ),
        (
            "financial analyst",
            "accountant",
            "finance manager",
            "accounts executive",
            "auditor",
            "chartered accountant",
            "bookkeep",
            "accounts payable",
            "accounts receivable",
        ),
        (
            "tally",
            "sap fico",
            "gaap",
            "ifrs",
            "financial modelling",
            "quickbooks",
            "auditing",
            "bank reconciliation",
            "accounts payable",
            "accounts receivable",
        ),
        ("data_analytics", "business_analysis", "operations"),
    ),
    _f(
        "investment_banking",
        "Investment Banking & Capital Markets",
        "Business",
        ("Investment Banking Analyst", "Equity Research Analyst", "Financial Modelling Analyst"),
        ("investment banking", "equity research", "capital markets", "m&a analyst"),
        ("financial modelling", "bloomberg", "equity research"),
        ("finance_accounting", "data_analytics"),
    ),
    _f(
        "marketing",
        "Marketing",
        "Business",
        (
            "Marketing Executive",
            "Digital Marketing Manager",
            "Marketing Analyst",
            "Brand Manager",
            "SEO Specialist",
            "Performance Marketing Manager",
        ),
        ("marketing", "seo", "sem", "brand manager", "growth marketer", "digital marketing"),
        (
            "google analytics",
            "seo",
            "sem",
            "google ads",
            "hubspot",
            "content marketing",
            "meta ads",
            "email marketing",
        ),
        ("content_writing", "sales", "data_analytics", "ux_design"),
    ),
    _f(
        "sales",
        "Sales & Business Development",
        "Business",
        (
            "Sales Executive",
            "Business Development Manager",
            "Account Manager",
            "Inside Sales Representative",
            "Sales Manager",
        ),
        ("sales", "business development", "account manager", "inside sales", "bdm", "key account"),
        ("salesforce", "negotiation", "lead generation", "b2b sales", "cold calling", "zoho crm"),
        ("marketing", "customer_success", "operations"),
    ),
    _f(
        "customer_success",
        "Customer Success & Support",
        "Business",
        (
            "Customer Success Manager",
            "Customer Support Executive",
            "Client Servicing Executive",
            "Technical Account Manager",
        ),
        (
            "customer success",
            "customer support",
            "client servicing",
            "customer service",
            "account management",
        ),
        ("salesforce", "zoho crm", "hubspot"),
        ("sales", "it_support", "operations"),
    ),
    _f(
        "human_resources",
        "Human Resources",
        "Business",
        (
            "HR Executive",
            "HR Generalist",
            "Talent Acquisition Specialist",
            "Recruiter",
            "HR Business Partner",
        ),
        (
            "human resources",
            "hr executive",
            "hr generalist",
            "talent acquisition",
            "recruiter",
            "hrbp",
            "payroll",
        ),
        (
            "recruitment",
            "onboarding",
            "payroll",
            "hrms",
            "employee engagement",
            "performance management",
        ),
        ("operations", "customer_success"),
    ),
    _f(
        "operations",
        "Operations",
        "Business",
        (
            "Operations Executive",
            "Operations Manager",
            "Business Operations Analyst",
            "Process Associate",
        ),
        (
            "operations executive",
            "operations manager",
            "business operations",
            "process associate",
            "operations analyst",
        ),
        ("process improvement", "sop", "six sigma"),
        ("supply_chain", "project_management", "business_analysis"),
    ),
    _f(
        "supply_chain",
        "Supply Chain & Logistics",
        "Business",
        (
            "Supply Chain Analyst",
            "Logistics Executive",
            "Procurement Executive",
            "Warehouse Manager",
            "Demand Planner",
        ),
        (
            "supply chain",
            "logistics",
            "procurement",
            "warehouse",
            "demand planning",
            "inventory",
            "sourcing executive",
        ),
        (
            "sap mm",
            "inventory management",
            "vendor management",
            "procurement",
            "logistics",
            "demand planning",
        ),
        ("operations", "manufacturing_engineering", "data_analytics"),
    ),
    _f(
        "consulting",
        "Consulting",
        "Business",
        ("Consultant", "Management Consultant", "Associate Consultant", "Strategy Analyst"),
        ("consultant", "consulting", "strategy analyst", "advisory"),
        ("stakeholder management", "process improvement"),
        ("business_analysis", "project_management", "finance_accounting"),
    ),
    _f(
        "legal",
        "Legal & Compliance",
        "Business",
        (
            "Legal Associate",
            "Company Secretary",
            "Compliance Officer",
            "Contract Manager",
            "Paralegal",
        ),
        (
            "legal",
            "advocate",
            "company secretary",
            "compliance officer",
            "contract manager",
            "paralegal",
            "counsel",
        ),
        ("contract drafting", "compliance", "litigation", "due diligence", "company secretary"),
        ("finance_accounting", "operations"),
    ),
)

# ─── Design, content, healthcare, education ─────────────────────────────────

OTHER: tuple[Family, ...] = (
    _f(
        "ux_design",
        "UX / UI Design",
        "Design",
        ("UX Designer", "UI Designer", "Product Designer", "UX Researcher", "Interaction Designer"),
        (
            "ux designer",
            "ui designer",
            "product designer",
            "ux researcher",
            "interaction design",
            "visual designer",
        ),
        ("figma", "adobe xd", "sketch", "wireframing", "prototyping", "user research"),
        ("frontend", "product_management", "graphic_design"),
    ),
    _f(
        "graphic_design",
        "Graphic & Motion Design",
        "Design",
        ("Graphic Designer", "Motion Graphics Designer", "Visual Designer", "Video Editor"),
        ("graphic designer", "motion graphics", "video editor", "creative designer"),
        ("photoshop", "illustrator", "after effects", "premiere pro", "canva", "indesign"),
        ("ux_design", "content_writing", "marketing"),
    ),
    _f(
        "content_writing",
        "Content & Communications",
        "Design",
        (
            "Content Writer",
            "Technical Writer",
            "Copywriter",
            "Content Strategist",
            "Communications Executive",
        ),
        (
            "content writer",
            "technical writer",
            "copywriter",
            "content strategist",
            "communications executive",
            "editor",
        ),
        ("seo", "copywriting", "wordpress", "technical writing"),
        ("marketing", "graphic_design"),
    ),
    _f(
        "healthcare_clinical",
        "Healthcare & Clinical",
        "Healthcare",
        (
            "Staff Nurse",
            "Clinical Research Associate",
            "Medical Officer",
            "Physiotherapist",
            "Lab Technician",
        ),
        (
            "nurse",
            "clinical research",
            "medical officer",
            "physiotherapist",
            "lab technician",
            "healthcare",
            "radiograph",
        ),
        (
            "patient care",
            "clinical trials",
            "glp",
            "phlebotomy",
            "emr",
            "icu",
            "radiography",
            "physiotherapy",
        ),
        ("pharma", "biotech"),
    ),
    _f(
        "pharma",
        "Pharmaceutical",
        "Healthcare",
        (
            "Pharmacist",
            "Regulatory Affairs Executive",
            "Pharmacovigilance Associate",
            "Production Pharmacist",
            "Quality Control Analyst Pharma",
        ),
        ("pharmacist", "regulatory affairs", "pharmacovigilance", "formulation", "pharma"),
        (
            "gmp",
            "hplc",
            "regulatory affairs",
            "pharmacovigilance",
            "method validation",
            "formulation",
        ),
        ("healthcare_clinical", "biotech", "chemical_engineering", "quality_engineering"),
    ),
    _f(
        "biotech",
        "Biotechnology & Life Sciences",
        "Healthcare",
        (
            "Research Associate Biotech",
            "Bioinformatics Analyst",
            "Microbiologist",
            "Biotechnologist",
        ),
        ("biotech", "bioinformatics", "microbiologist", "molecular biology", "life sciences"),
        ("pcr", "bioinformatics", "cell culture", "python", "genomics", "microbiology"),
        ("pharma", "healthcare_clinical", "data_science"),
    ),
    _f(
        "education",
        "Education & Training",
        "Education",
        (
            "Teacher",
            "Lecturer",
            "Assistant Professor",
            "Corporate Trainer",
            "Instructional Designer",
        ),
        (
            "teacher",
            "lecturer",
            "professor",
            "trainer",
            "instructional designer",
            "faculty",
            "academic",
        ),
        ("curriculum", "lesson planning", "e-learning", "instructional design"),
        ("content_writing", "human_resources"),
    ),
    _f(
        "hospitality",
        "Hospitality & Facilities",
        "Services",
        (
            "Hotel Manager",
            "Front Office Executive",
            "Facility Manager",
            "Chef",
            "Housekeeping Supervisor",
        ),
        (
            "hotel",
            "hospitality",
            "front office",
            "facility manager",
            "chef",
            "housekeeping",
            "restaurant manager",
        ),
        ("guest relations", "food safety", "facility management"),
        ("operations", "customer_success"),
    ),
)

FAMILIES: tuple[Family, ...] = TECHNOLOGY + ENGINEERING + BUSINESS + OTHER
BY_ID: dict[str, Family] = {f.id: f for f in FAMILIES}

GROUPS: tuple[str, ...] = (
    "Technology",
    "Engineering",
    "Business",
    "Design",
    "Healthcare",
    "Education",
    "Services",
)


def get(family_id: str) -> Family | None:
    return BY_ID.get((family_id or "").strip().lower())


def by_group() -> dict[str, list[Family]]:
    """Families grouped for the picker, in a stable order."""
    out: dict[str, list[Family]] = {group: [] for group in GROUPS}
    for family in FAMILIES:
        out.setdefault(family.group, []).append(family)
    return {group: members for group, members in out.items() if members}


# A marker that opens the title is a qualifier, and a qualifier is the
# specialisation: "Embedded Software Engineer" is an embedded role that happens to be
# software. Worth more than the eight characters it is long.
_LEADING_BONUS = 10


def for_title(title: str) -> Family | None:
    """The family a job title belongs to.

    Ranked by (marker length + a bonus for leading the title, then earliest position).
    Each part of that earns its place against a real title:

      * **Length** stops "Machine Learning Engineer" being captured by the bare
        "engineer"-shaped markers in a dozen other families.
      * **Leading bonus** fixes "Embedded Software Engineer", where the specialisation
        is the *shorter* word and the generic match is longer.
      * **Earliest position** breaks exact ties by phrase head: "VLSI Physical Design
        Engineer" matches both "physical design" and "design engineer" at fifteen
        characters each, and the first one is what the job is.
    """
    normalized = f" {' '.join((title or '').lower().split())} "
    best: tuple[int, int, Family | None] = (0, 0, None)
    for family in FAMILIES:
        for marker in family.title_markers:
            position = normalized.find(marker)
            if position < 0:
                continue
            # Position 1 is the start of the title: `normalized` is space-padded.
            weight = len(marker) + (_LEADING_BONUS if position <= 1 else 0)
            score = (weight, -position)
            if score > best[:2]:
                best = (weight, -position, family)
    return best[2]


def expand(family_ids: list[str], include_related: bool = True) -> list[Family]:
    """The families to actually search for.

    `include_related` is the "and then more related jobs" behaviour: choosing Data
    Science also searches Data Analytics and ML Engineering. Related families are
    appended after the explicit ones, so the user's own choices stay first when a
    result cap bites.
    """
    chosen: list[Family] = []
    seen: set[str] = set()

    for family_id in family_ids:
        family = get(family_id)
        if family and family.id not in seen:
            chosen.append(family)
            seen.add(family.id)

    if not include_related:
        return chosen

    for family in list(chosen):
        for related_id in family.related:
            related = get(related_id)
            if related and related.id not in seen:
                chosen.append(related)
                seen.add(related.id)
    return chosen


def search_queries(families: list[Family], limit_per_family: int = 3) -> list[str]:
    """Deduplicated query phrases to send to job boards.

    Capped per family because every query is an API call against a metered free tier,
    and the third phrasing of "Data Analyst" returns mostly what the first two did.
    """
    out: list[str] = []
    seen: set[str] = set()
    for family in families:
        for query in family.queries[:limit_per_family]:
            key = query.lower()
            if key not in seen:
                out.append(query)
                seen.add(key)
    return out
