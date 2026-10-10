"""Built-in starting vocabulary for the search pickers, so they are useful
before the first catalog scan (and offline). Scans add whatever the market
actually uses on top of this; nothing here is ever deleted by a scan.
"""
from __future__ import annotations

# (name, code) -- names spelled the way Adzuna's location tree spells them.
CANADIAN_PROVINCES: list[tuple[str, str]] = [
    ("Ontario", "ON"), ("Québec", "QC"), ("British Columbia", "BC"), ("Alberta", "AB"),
    ("Manitoba", "MB"), ("Saskatchewan", "SK"), ("Nova Scotia", "NS"), ("New Brunswick", "NB"),
    ("Newfoundland and Labrador", "NL"), ("Prince Edward Island", "PE"),
    ("Northwest Territories", "NT"), ("Yukon", "YT"), ("Nunavut", "NU"),
]
PROVINCE_CODES: dict[str, str] = {name: code for name, code in CANADIAN_PROVINCES}

# Major Canadian job markets as (city, province code). Their exact location
# path is filled in by a scan; until then they are searched by name.
CANADIAN_CITIES: list[tuple[str, str]] = [
    ("Toronto", "ON"), ("Ottawa", "ON"), ("Mississauga", "ON"), ("Brampton", "ON"), ("Markham", "ON"),
    ("Vaughan", "ON"), ("Oakville", "ON"), ("Burlington", "ON"), ("Hamilton", "ON"), ("Kitchener", "ON"),
    ("Waterloo", "ON"), ("Cambridge", "ON"), ("Guelph", "ON"), ("London", "ON"), ("Windsor", "ON"),
    ("Kingston", "ON"), ("Richmond Hill", "ON"), ("Barrie", "ON"),
    ("Montréal", "QC"), ("Québec City", "QC"), ("Laval", "QC"), ("Gatineau", "QC"), ("Sherbrooke", "QC"),
    ("Vancouver", "BC"), ("Victoria", "BC"), ("Burnaby", "BC"), ("Surrey", "BC"), ("Richmond", "BC"),
    ("Coquitlam", "BC"), ("Langley", "BC"), ("North Vancouver", "BC"), ("Kelowna", "BC"), ("Kamloops", "BC"),
    ("Nanaimo", "BC"), ("Abbotsford", "BC"),
    ("Calgary", "AB"), ("Edmonton", "AB"), ("Red Deer", "AB"), ("Lethbridge", "AB"),
    ("Winnipeg", "MB"), ("Saskatoon", "SK"), ("Regina", "SK"),
    ("Halifax", "NS"), ("Moncton", "NB"), ("Fredericton", "NB"), ("Saint John", "NB"),
    ("St. John's", "NL"), ("Charlottetown", "PE"), ("Whitehorse", "YT"), ("Yellowknife", "NT"),
]

REMOTE_LOCATIONS: list[str] = ["Remote"]  # "Hybrid" is a keyword: Adzuna has no place for it

JOB_TITLES: list[str] = [
    # Software
    "Software Engineer", "Senior Software Engineer", "Staff Software Engineer", "Principal Software Engineer",
    "Software Developer", "Senior Software Developer", "Backend Engineer", "Senior Backend Engineer",
    "Backend Developer", "Frontend Engineer", "Senior Frontend Engineer", "Frontend Developer",
    "Full Stack Developer", "Full Stack Engineer", "Senior Full Stack Developer", "Java Developer",
    "Senior Java Developer", "Java Engineer", "Python Developer", "Senior Python Developer", ".NET Developer",
    "C# Developer", "JavaScript Developer", "React Developer", "Node.js Developer", "Go Developer",
    "Ruby on Rails Developer", "PHP Developer", "Mobile Developer", "iOS Developer", "Android Developer",
    "Embedded Software Engineer", "Firmware Engineer", "Game Developer", "Web Developer",
    # Platform / ops / security
    "DevOps Engineer", "Senior DevOps Engineer", "Site Reliability Engineer", "Platform Engineer",
    "Cloud Engineer", "Cloud Architect", "Solutions Architect", "Software Architect", "Enterprise Architect",
    "Infrastructure Engineer", "Network Engineer", "Systems Administrator", "Database Administrator",
    "Security Engineer", "Cybersecurity Analyst", "Information Security Analyst", "Penetration Tester",
    # Data / AI
    "Data Engineer", "Senior Data Engineer", "Data Scientist", "Senior Data Scientist", "Data Analyst",
    "Business Intelligence Analyst", "Machine Learning Engineer", "AI Engineer", "MLOps Engineer",
    "Analytics Engineer",
    # Quality / product / design
    "QA Engineer", "QA Analyst", "Test Automation Engineer", "Software Development Engineer in Test",
    "Product Manager", "Senior Product Manager", "Technical Product Manager", "Product Owner",
    "UX Designer", "UI Designer", "Product Designer", "UX Researcher",
    # Leadership
    "Engineering Manager", "Senior Engineering Manager", "Director of Engineering", "VP of Engineering",
    "Technical Lead", "Tech Lead", "Team Lead", "CTO", "Head of Engineering", "Scrum Master",
    "Agile Coach", "Project Manager", "Technical Project Manager", "Program Manager", "Delivery Manager",
    # Business / IT
    "Business Analyst", "Systems Analyst", "IT Support Specialist", "Help Desk Technician", "IT Manager",
    "Technical Writer", "Sales Engineer", "Customer Success Manager", "Implementation Specialist",
    "Technical Support Engineer", "ERP Consultant", "Salesforce Developer", "SAP Consultant",
    # Other common professions
    "Accountant", "Financial Analyst", "Marketing Manager", "Digital Marketing Specialist",
    "Human Resources Generalist", "Recruiter", "Registered Nurse", "Mechanical Engineer",
    "Electrical Engineer", "Civil Engineer", "Administrative Assistant", "Customer Service Representative",
    "Sales Representative", "Account Manager", "Operations Manager", "Office Manager",
]

KEYWORDS: list[str] = [
    # Languages
    "Java", "Kotlin", "Scala", "Python", "Go", "Rust", "C", "C++", "C#", ".NET", "JavaScript", "TypeScript",
    "Ruby", "PHP", "Swift", "Objective-C", "SQL", "Bash", "R",
    # Backend frameworks
    "Spring", "Spring Boot", "Quarkus", "Micronaut", "Hibernate", "Django", "Flask", "FastAPI", "Node.js",
    "Express", "NestJS", "Ruby on Rails", "Laravel", "ASP.NET",
    # Frontend
    "React", "Angular", "Vue", "Next.js", "Redux", "HTML", "CSS", "Tailwind",
    # Architecture
    "Microservices", "REST", "GraphQL", "gRPC", "Event-Driven Architecture", "Domain-Driven Design",
    "Distributed Systems", "System Design", "API Design", "Serverless",
    # Messaging / data stores
    "Kafka", "RabbitMQ", "ActiveMQ", "Pulsar", "Redis", "PostgreSQL", "MySQL", "Oracle", "SQL Server",
    "MongoDB", "Cassandra", "DynamoDB", "Elasticsearch", "Snowflake", "BigQuery",
    # Cloud / ops
    "AWS", "Azure", "GCP", "Docker", "Kubernetes", "Terraform", "Ansible", "Helm", "OpenShift", "Linux",
    "CI/CD", "Jenkins", "GitHub Actions", "GitLab CI", "ArgoCD", "Prometheus", "Grafana", "Datadog",
    "Splunk", "New Relic", "OpenTelemetry", "Observability",
    # Data / AI
    "Spark", "Hadoop", "Airflow", "dbt", "Databricks", "Pandas", "TensorFlow", "PyTorch", "Machine Learning",
    "Deep Learning", "LLM", "Generative AI", "NLP", "Computer Vision", "Data Pipelines", "ETL",
    # Practices
    "Agile", "Scrum", "TDD", "Unit Testing", "JUnit", "Selenium", "Cypress", "Playwright", "Git",
    "Code Review", "Mentoring", "Leadership", "Security", "OAuth", "Performance Tuning", "Scalability",
    # Domains / work style
    "FinTech", "Payments", "Banking", "Insurance", "Healthcare", "E-commerce", "SaaS", "Telecom",
    "Government", "Startup", "Remote", "Hybrid", "Contract", "Full-time", "Part-time",
]
