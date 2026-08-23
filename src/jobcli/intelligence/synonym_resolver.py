"""Synonym resolver for matching form field labels and values across ATS variants.

Handles cases like:
  - Form says "Man" but resume JSON says "Male"
  - Form label is "Pronouns" but our data key is "gender"
  - Form says "Given Name" but our key is "first_name"
"""

import re
from typing import Any, Optional

from jobcli.profile.derived_profile import derived_country_for_resume, derived_pronouns_for_resume
from jobcli.profile.resume_normalize import normalize_linkedin_url
from jobcli.profile.schemas import CommonQuestions, ResumeData, coerce_gpa_value


def _pick_education_gpa(low_edu: dict[str, Any]) -> Optional[float]:
    """Use the first parseable GPA among score/gpa keys (resume builders often duplicate)."""
    for key in ("score", "gpa", "grade point", "cumulative gpa"):
        parsed = coerce_gpa_value(low_edu.get(key))
        if parsed is not None:
            return parsed
    return None


class SynonymResolver:
    """Resolve field labels AND dropdown values to handle ATS site variations."""

    def __init__(self, infer_location_country: bool = True) -> None:
        self.infer_location_country = infer_location_country

    # ── Label Synonyms: map form labels → our internal keys ───────────────
    # Key = our internal field key
    # Value = list of lowercase substrings that identify this field
    LABEL_SYNONYMS: dict[str, list[str]] = {
        # Personal info
        "first_name": [
            "first name", "given name", "forename", "nombre",
            "first", "fname",
        ],
        "last_name": [
            "last name", "surname", "family name", "apellido",
            "last", "lname",
        ],
        "full_name": [
            "full name", "your name", "legal name", "name",
        ],
        "email": [
            "email", "e-mail", "correo", "email address",
        ],
        "phone": [
            "phone", "mobile", "cell", "telephone",
            "contact number", "phone number", "mobile number",
        ],
        "linkedin": [
            "linkedin", "linkedin url", "linkedin profile",
        ],
        "github": [
            "github", "github url", "github profile",
        ],
        "portfolio": [
            "portfolio", "website", "personal website", "personal site",
        ],
        "location": [
            "location", "current location", "where are you based",
            "city, state", "current city",
        ],
        "city": ["city"],
        "state": ["state", "province"],
        "country": ["country"],
        "zip_code": ["zip", "postal", "zip code", "postal code"],
        "address": ["address", "street address", "mailing address"],

        # Demographics
        "gender": [
            "gender", "sex", "gender identity",
            "what is your gender", "gender (optional)",
        ],
        "pronouns": [
            "pronouns", "preferred pronouns", "what are your pronouns",
            "your pronouns", "pronoun",
        ],
        "sexual_orientation": [
            "sexual orientation", "orientation", "lgbtq",
            "are you a member of the lgbtq",
        ],
        "race": [
            "race", "ethnicity", "race/ethnicity",
            "racial", "ethnic background",
        ],
        "veteran": [
            "veteran", "veteran status", "protected veteran",
            "are you a veteran",
        ],
        "disability": [
            "disability", "disability status",
            "do you have a disability",
        ],

        # Work authorization
        "work_auth": [
            "authorized to work", "work authorization",
            "legally authorized", "right to work",
            "are you legally", "eligible to work",
            "authorized", "legal right",
        ],
        "sponsorship": [
            "sponsorship", "visa sponsorship",
            "require sponsorship", "need sponsorship",
            "immigration sponsorship",
        ],

        # Compensation & availability
        "salary": [
            "salary", "desired salary", "salary expectation",
            "expected salary", "compensation", "pay expectation",
            "desired compensation",
        ],
        "start_date": [
            "start date", "available start", "earliest start",
            "when can you start", "availability",
        ],
        "notice_period": [
            "notice period", "notice", "current notice",
        ],

        # Referral
        "how_heard": [
            "how did you hear", "where did you hear",
            "referral source", "how did you find",
            "how did you learn", "source",
        ],
        "referral": [
            "referral", "referred by", "who referred",
            "referral name",
        ],

        # Education
        "education_school": [
            "school", "university", "college", "institution",
            "school name", "university name",
        ],
        "education_degree": [
            "degree", "degree type", "level of education",
            "education level", "highest degree",
        ],
        "education_field": [
            "field of study", "major", "discipline",
            "area of study", "concentration",
        ],
        "education_gpa": [
            "gpa", "grade point", "cumulative gpa",
        ],
        "graduation_year": [
            "graduation", "grad year", "year of graduation",
            "expected graduation", "graduation date",
        ],

        # Experience
        "experience_company": [
            "company",
            "employer",
            "organization",
            "company name",
            "employer name",
            "name of employer",
            "organization name",
        ],
        "experience_title": [
            "title",
            "job title",
            "role",
            "position",
            "position title",
            "job role",
            "your title",
            "position/role",
        ],
        "experience_start": [
            "start date",
            "from",
            "start",
            "date started",
        ],
        "experience_end": [
            "end date",
            "to",
            "end",
            "date ended",
        ],
        "experience_description": [
            "description",
            "job description",
            "responsibilities",
            "duties",
            "summary",
            "roles and responsibilities",
            "role description",
            "work performed",
        ],

        # Resume/CV upload
        "resume_upload": [
            "resume", "cv", "curriculum vitae",
            "upload resume", "upload cv", "attach resume",
        ],
        "cover_letter": [
            "cover letter", "cover", "motivation letter",
        ],

        # EEO / optional
        "willing_to_relocate": [
            "relocate", "willing to relocate", "relocation",
            "open to relocation",
        ],
        "remote_preference": [
            "remote", "work preference", "work arrangement",
            "onsite", "hybrid",
        ],
    }

    # ── Value Synonyms: map our values → variations sites may use ─────────
    # Key = canonical lowercase value from our data
    # Value = list of equivalent strings (all lowercase)
    VALUE_SYNONYMS: dict[str, list[str]] = {
        # Gender
        "male": ["male", "man", "m", "masculine", "he/him", "he/him/his", "he"],
        "female": ["female", "woman", "f", "feminine", "she/her", "she/her/hers", "she"],
        "non-binary": [
            "non-binary", "nonbinary", "non binary", "they/them",
            "genderqueer", "genderfluid", "gender non-conforming",
            "they/them/theirs",
        ],
        "prefer not to say": [
            "prefer not to say", "decline to self-identify",
            "decline", "prefer not to answer",
            "i don't wish to answer", "choose not to disclose",
            "not specified", "rather not say",
        ],

        # Yes/No
        "yes": ["yes", "y", "true", "si", "i am", "authorized"],
        "no": ["no", "n", "false", "not", "i am not"],

        # Work authorization
        "authorized": [
            "yes", "authorized", "i am authorized",
            "legally authorized", "yes, i am authorized",
        ],
        "not authorized": [
            "no", "not authorized", "i am not authorized",
        ],

        # Sponsorship
        "no sponsorship": [
            "no", "no sponsorship required", "do not require",
            "will not require", "n/a", "will not now or in the future",
        ],
        "needs sponsorship": [
            "yes", "require sponsorship", "will require",
            "yes, now or in the future",
        ],

        # Veteran
        "not a veteran": [
            "i am not a protected veteran", "no",
            "not a veteran", "not a protected veteran",
        ],
        "veteran": [
            "yes", "i am a protected veteran", "veteran",
            "i identify as one or more of the classifications of a protected veteran",
        ],
        "prefer not to answer veteran": [
            "i don't wish to answer", "prefer not to answer",
            "decline to self-identify",
        ],

        # Disability
        "no disability": [
            "no, i don't have a disability",
            "no, i do not have a disability",
            "no", "i don't have a disability",
            "no disability",
        ],
        "has disability": [
            "yes, i have a disability",
            "yes", "i have a disability",
        ],
        "prefer not to answer disability": [
            "i don't wish to answer", "prefer not to answer",
            "decline to self-identify",
        ],

        # Race/ethnicity
        "asian": ["asian", "asian / pacific islander", "asian or pacific islander"],
        "white": ["white", "caucasian", "white / caucasian"],
        "black": ["black", "african american", "black or african american"],
        "hispanic": ["hispanic", "latino", "hispanic or latino", "latinx"],
        "two or more": ["two or more races", "multiracial", "mixed"],
        "prefer not to answer race": [
            "decline to self-identify", "prefer not to say",
            "prefer not to answer", "i don't wish to answer",
        ],

        # Sexual orientation (form wording → canonical bucket keys)
        "heterosexual": [
            "heterosexual", "straight", "hetero", "hetro sexual", "hetro",
        ],
        "straight": [
            "straight", "heterosexual", "hetero", "hetro",
        ],
        "prefer not to answer orientation": [
            "decline to self-identify", "prefer not to say",
            "prefer not to answer orientation",
        ],

        # Remote preference
        "remote": ["remote", "fully remote", "100% remote", "work from home"],
        "hybrid": ["hybrid", "flexible", "mix of remote and onsite"],
        "onsite": ["onsite", "on-site", "in-office", "in office"],
    }

    def resolve_field_label(self, form_label: str) -> Optional[str]:
        """Map a form field label to our internal key.

        Examples:
            "What is your Gender?" → "gender"
            "Given Name" → "first_name"
            "He/Him pronouns" → "gender"
            "School name *" → "education_school"

        Returns:
            Internal key string, or None if no match.
        """
        normalized = form_label.lower().strip()
        # Remove common suffixes: *, (optional), (required)
        normalized = re.sub(r"\s*[\*\(\)].*$", "", normalized).strip()

        # Step 3: Fuzzy / Keyword matching
        # Calculate how many keywords from LABEL_SYNONYMS appear in the label
        best_keyword_match: Optional[str] = None
        max_keywords = 0
        
        for key, synonyms in self.LABEL_SYNONYMS.items():
            for syn in synonyms:
                # Use word boundaries for better accuracy
                if re.search(r'\b' + re.escape(syn) + r'\b', normalized):
                    if len(syn.split()) > max_keywords:
                        best_keyword_match = key
                        max_keywords = len(syn.split())
        
        if best_keyword_match:
            return best_keyword_match

        return None

    def find_best_option(
        self, our_value: str, dropdown_options: list[str]
    ) -> Optional[str]:
        """Match our value against available dropdown options using synonyms.

        Examples:
            our_value="Male", options=["Man", "Woman", "Non-binary"] → "Man"
            our_value="Male", options=["M", "F", "Other"] → "M"
            our_value="Yes", options=["I am authorized", "No"] → "I am authorized"

        Returns:
            The exact option string to use, or None if no match.
        """
        if not our_value or not dropdown_options:
            return None

        our_lower = our_value.lower().strip()

        # Step 1: Exact match (case-insensitive)
        for option in dropdown_options:
            if option.lower().strip() == our_lower:
                return option

        # Step 2: Collect all synonyms for our value
        synonyms: set[str] = {our_lower}
        for _key, syns in self.VALUE_SYNONYMS.items():
            lower_syns = [s.lower() for s in syns]
            if our_lower in lower_syns:
                synonyms.update(lower_syns)

        # Step 3: Exact synonym match against options
        for option in dropdown_options:
            opt_lower = option.lower().strip()
            if opt_lower in synonyms:
                return option

        # Step 4: Substring / partial match
        for option in dropdown_options:
            opt_lower = option.lower().strip()
            for syn in synonyms:
                if syn in opt_lower or opt_lower in syn:
                    # Avoid matching empty or trivially short strings
                    if len(syn) >= 2 and len(opt_lower) >= 2:
                        return option

        return None

    def get_resume_value(
        self,
        field_key: str,
        resume: ResumeData,
        common_questions: Optional[CommonQuestions] = None,
    ) -> Optional[str]:
        """Get the value from ResumeData for a normalized field key.

        This is the FIRST PRIORITY data source.

        Args:
            field_key: Internal field key (from resolve_field_label).
            resume: The user's resume data.
            common_questions: Optional pre-set answers from ``wboxcli questions``.

        Returns:
            Value string, or None if not available.
        """
        if common_questions:
            cq_val = self._common_questions_value(field_key, common_questions)
            if cq_val:
                return cq_val

        mapping: dict[str, Any] = {
            "first_name": lambda r: r.personal.first_name,
            "last_name": lambda r: r.personal.last_name,
            "full_name": lambda r: f"{r.personal.first_name} {r.personal.last_name}",
            "email": lambda r: r.personal.email,
            "phone": lambda r: r.personal.phone,
            "linkedin": lambda r: normalize_linkedin_url(r.personal.linkedin),
            "github": lambda r: r.personal.github,
            "portfolio": lambda r: r.personal.portfolio or r.personal.website,
            "address": lambda r: r.personal.address,
            "city": lambda r: r.personal.city,
            "state": lambda r: r.personal.state,
            "country": lambda r: r.personal.country,
            "zip_code": lambda r: r.personal.zip_code,
            "location": lambda r: (
                f"{r.personal.city}, {r.personal.state}"
                if r.personal.city and r.personal.state
                else r.personal.city or r.personal.state
            ),
            # Demographics
            "gender": lambda r: r.demographics.gender if r.demographics else None,
            "pronouns": lambda r: SynonymResolver._pronouns_value(r),
            "sexual_orientation": lambda r: (
                r.demographics.sexual_orientation if r.demographics else None
            ),
            "race": lambda r: r.demographics.race if r.demographics else None,
            "veteran": lambda r: (
                r.demographics.veteran_status if r.demographics else None
            ),
            "disability": lambda r: (
                r.demographics.disability_status if r.demographics else None
            ),
            # Work authorization
            "work_auth": lambda r: (
                "Yes" if r.work_authorization.authorized_to_work else "No"
            ),
            "sponsorship": lambda r: (
                "Yes" if r.work_authorization.require_sponsorship else "No"
            ),
            # Education (first entry)
            "education_school": lambda r: (
                r.education[0].school if r.education else None
            ),
            "education_degree": lambda r: (
                r.education[0].degree if r.education else None
            ),
            "education_field": lambda r: (
                r.education[0].field_of_study if r.education else None
            ),
            "education_gpa": lambda r: (
                str(r.education[0].gpa) if r.education and r.education[0].gpa else None
            ),
            "graduation_year": lambda r: (
                str(r.education[0].graduation_year) if r.education else None
            ),
            # Experience (first/most recent entry)
            "experience_company": lambda r: (
                r.experience[0].company if r.experience else None
            ),
            "experience_title": lambda r: (
                r.experience[0].title if r.experience else None
            ),
            "experience_start": lambda r: (
                r.experience[0].start_date if r.experience else None
            ),
            "experience_end": lambda r: (
                r.experience[0].end_date if r.experience else None
            ),
            "experience_description": lambda r: (
                (r.experience[0].description or "").strip()[:8000]
                if r.experience and r.experience[0].description
                else None
            ),
            # Resume path (handled separately by upload action)
            "resume_upload": lambda r: None,
            "cover_letter": lambda r: None,
        }

        getter = mapping.get(field_key)
        if not getter:
            return None

        try:
            value = getter(resume)
            if field_key == "country" and (
                value is None or not str(value).strip()
            ) and self.infer_location_country:
                inferred = derived_country_for_resume(resume)
                if inferred:
                    value = inferred
            return str(value) if value is not None else None
        except (IndexError, AttributeError):
            return None

    @staticmethod
    def _common_questions_value(
        field_key: str, common_questions: CommonQuestions
    ) -> Optional[str]:
        """Map canonical field keys to ``CommonQuestions`` CLI answers."""
        cq_map: dict[str, Any] = {
            "notice_period": common_questions.notice_period,
            "salary": common_questions.salary_expectations,
            "start_date": common_questions.start_date,
            "referral": common_questions.referral,
            "cover_letter": common_questions.cover_letter,
            "willing_to_relocate": common_questions.willing_to_relocate,
            "remote_preference": common_questions.remote_preference,
        }
        raw = cq_map.get(field_key)
        if raw is None:
            return None
        if isinstance(raw, bool):
            return "Yes" if raw else "No"
        text = str(raw).strip()
        return text or None

    @staticmethod
    def _pronouns_value(resume: ResumeData) -> Optional[str]:
        demo = resume.demographics
        explicit = demo.pronouns if demo else None
        if explicit and str(explicit).strip():
            return explicit
        derived = derived_pronouns_for_resume(resume)
        return derived


class ResumeAutoDetector:
    """Auto-detect and normalize different resume JSON formats.

    Handles:
      - Our native format: {personal: {first_name, last_name, ...}}
      - JSONResume format:  {basics: {name, email, phone, ...}}
      - Flat format:        {first_name, last_name, email, ...}
    """

    @staticmethod
    def _deep_lowercase_keys(data: Any) -> Any:
        """Recursively lowercase all keys in a dictionary."""
        if isinstance(data, dict):
            return {k.lower().replace(" ", "_"): ResumeAutoDetector._deep_lowercase_keys(v) for k, v in data.items()}
        if isinstance(data, list):
            return [ResumeAutoDetector._deep_lowercase_keys(i) for i in data]
        return data

    @staticmethod
    def detect_and_convert(raw_json: dict[str, Any]) -> dict[str, Any]:
        """Detect the resume JSON format and convert to our native schema.

        Args:
            raw_json: Raw parsed JSON from any format.

        Returns:
            Dict matching our ResumeData schema.
        """
        # Recursively lowercase and normalize keys (e.g., "First Name" -> "first_name")
        data = ResumeAutoDetector._deep_lowercase_keys(raw_json)

        # Already our format?
        if "personal" in data and isinstance(data["personal"], dict):
            return data

        # JSONResume format (bavish.json style)
        if "basics" in data:
            return ResumeAutoDetector._convert_json_resume(data)

        # Flat format
        if "first_name" in data or "name" in data:
            return ResumeAutoDetector._convert_flat(data)

        # Unknown — return as-is and let validation catch errors
        return data

    @staticmethod
    def _convert_json_resume(data: dict[str, Any]) -> dict[str, Any]:
        """Convert JSONResume format to our schema."""
        # This receives low_json from detect_and_convert
        basics = data.get("basics", {})
        low_basics = {k.lower(): v for k, v in basics.items()} if isinstance(basics, dict) else {}
        
        location = low_basics.get("location", {})
        low_location = {k.lower(): v for k, v in location.items()} if isinstance(location, dict) else {}
        
        profiles = low_basics.get("profiles", [])

        # Parse name
        full_name = low_basics.get("name", "")
        name_parts = str(full_name).strip().split(" ", 1)
        first_name = name_parts[0] if name_parts else ""
        last_name = name_parts[1] if len(name_parts) > 1 else ""

        # Extract profile URLs
        linkedin = ""
        github = ""
        portfolio = ""
        for profile in profiles:
            if not isinstance(profile, dict): continue
            network = (profile.get("network", "") or "").lower()
            url = profile.get("url", "")
            if "linkedin" in network:
                linkedin = url
            elif "github" in network:
                github = url
            elif network in ("portfolio", "website", "personal"):
                portfolio = url

        main_url = str(low_basics.get("website") or low_basics.get("url") or "").strip()
        if not linkedin and "linkedin.com" in main_url.lower():
            linkedin = main_url
        if not github and "github.com" in main_url.lower():
            github = main_url

        personal = {
            "first_name": first_name,
            "last_name": last_name,
            "email": low_basics.get("email", ""),
            "phone": low_basics.get("phone", ""),
            "city": low_location.get("city", ""),
            "state": low_location.get("region", low_location.get("state", "")),
            "country": low_location.get("countrycode", low_location.get("country", "")),
            "zip_code": low_location.get("postalcode", ""),
            "address": low_location.get("address", ""),
            "linkedin": linkedin,
            "github": github,
            "portfolio": portfolio,
            "website": main_url,
        }

        # Education
        education = []
        for edu in data.get("education", []):
            if not isinstance(edu, dict): continue
            low_edu = {k.lower(): v for k, v in edu.items()}
            education.append({
                "school": low_edu.get("institution", low_edu.get("school", "")),
                "degree": low_edu.get("studytype", low_edu.get("degree", "")),
                "field_of_study": low_edu.get("area", low_edu.get("field_of_study", "")),
                "graduation_year": ResumeAutoDetector._parse_year(
                    low_edu.get("enddate", low_edu.get("graduation_year", ""))
                ),
                "gpa": _pick_education_gpa(low_edu),
            })

        # Experience
        experience = []
        for work in data.get("work", data.get("experience", [])):
            if not isinstance(work, dict): continue
            low_work = {k.lower(): v for k, v in work.items()}
            experience.append({
                "company": low_work.get("name", low_work.get("company", "")),
                "title": low_work.get("position", low_work.get("title", "")),
                "start_date": low_work.get("startdate", low_work.get("start_date", "")),
                "end_date": low_work.get("enddate", low_work.get("end_date", "")),
                "current": not bool(low_work.get("enddate", low_work.get("end_date", ""))),
                "description": low_work.get("summary", low_work.get("description", "")),
            })

        # Skills
        skills: list[str] = []
        for skill_group in data.get("skills", []):
            if isinstance(skill_group, dict):
                low_sg = {k.lower(): v for k, v in skill_group.items()}
                keywords = low_sg.get("keywords", [])
                if isinstance(keywords, list):
                    for kw in keywords:
                        if isinstance(kw, str) and kw.strip():
                            skills.append(kw.strip())
                        elif kw:
                            skills.append(str(kw).strip())
                name = low_sg.get("name", "")
                if name and str(name).strip() not in skills:
                    skills.append(str(name).strip())
            elif isinstance(skill_group, str):
                if skill_group.strip():
                    skills.append(skill_group.strip())
            elif skill_group is not None:
                skills.append(str(skill_group).strip())

        # Certifications
        certifications = []
        for cert in data.get("certificates", data.get("certifications", [])):
            if isinstance(cert, dict):
                low_c = {k.lower(): v for k, v in cert.items()}
                certifications.append(low_c.get("name", str(cert)))
            elif isinstance(cert, str):
                certifications.append(cert)

        # Demographics
        demographics = None
        demo_data = data.get("demographics", {})
        if demo_data:
            low_demo = {k.lower(): v for k, v in demo_data.items()} if isinstance(demo_data, dict) else {}
            demographics = {
                "gender": low_demo.get("gender"),
                "race": low_demo.get("race"),
                "veteran_status": low_demo.get("veteran_status"),
                "disability_status": low_demo.get("disability_status"),
            }

        # Work authorization
        work_auth = data.get("work_authorization", {})
        low_wa = {k.lower(): v for k, v in work_auth.items()} if isinstance(work_auth, dict) else {}
        work_authorization = {
            "authorized_to_work": low_wa.get("authorized_to_work", True),
            "require_sponsorship": low_wa.get("require_sponsorship", False),
            "visa_status": low_wa.get("visa_status"),
        }

        result: dict[str, Any] = {
            "personal": personal,
            "education": education,
            "experience": experience,
            "skills": skills,
            "certifications": certifications,
            "work_authorization": work_authorization,
        }
        if demographics:
            result["demographics"] = demographics

        return result

    @staticmethod
    def _convert_flat(data: dict[str, Any]) -> dict[str, Any]:
        """Convert flat format to our schema."""
        # data is already lowercase keys from detect_and_convert
        
        # Parse name if only 'name' is provided
        full_name = data.get("name", "")
        if full_name and "first_name" not in data:
            parts = str(full_name).strip().split(" ", 1)
            first_name = parts[0]
            last_name = parts[1] if len(parts) > 1 else ""
        else:
            first_name = data.get("first_name", "")
            last_name = data.get("last_name", "")

        personal = {
            "first_name": first_name,
            "last_name": last_name,
            "email": data.get("email", ""),
            "phone": data.get("phone", ""),
            "city": data.get("city", ""),
            "state": data.get("state", ""),
            "country": data.get("country", ""),
            "linkedin": data.get("linkedin", ""),
            "github": data.get("github", ""),
        }

        return {
            "personal": personal,
            "education": data.get("education", []),
            "experience": data.get("experience", []),
            "skills": data.get("skills", []),
            "work_authorization": data.get("work_authorization", {
                "authorized_to_work": True,
                "require_sponsorship": False,
            }),
        }

    @staticmethod
    def _parse_year(value: Any) -> int:
        """Extract year from various date formats."""
        if isinstance(value, int):
            return value
        if isinstance(value, str):
            # Try extracting 4-digit year
            match = re.search(r"(\d{4})", str(value))
            if match:
                return int(match.group(1))
        return 0
