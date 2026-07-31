"""Mappers to convert between resume JSON and canonical ApplicationField.

This is the bridge between the user's resume.json and the canonical model.
Every FieldSemanticType should have a mapping rule here.
"""

from typing import Optional

from jobcli.canonical.models import FieldSemanticType, FieldSource
from jobcli.profile.schemas import ResumeData


class ResumeFieldMapper:
    """Maps FieldSemanticType to resume JSON paths.

    Usage:
        mapper = ResumeFieldMapper(resume)
        email = mapper.get_value(FieldSemanticType.EMAIL)
    """

    def __init__(self, resume: ResumeData):
        self.resume = resume

    def get_value(self, semantic_type: FieldSemanticType) -> Optional[str]:
        """Get the value for a semantic type from resume JSON.

        Returns None if the field doesn't exist in the resume.
        """
        # Personal Identity
        if semantic_type == FieldSemanticType.EMAIL:
            return self.resume.personal.email

        elif semantic_type == FieldSemanticType.PHONE:
            return self.resume.personal.phone

        elif semantic_type == FieldSemanticType.FIRST_NAME:
            return self.resume.personal.first_name

        elif semantic_type == FieldSemanticType.LAST_NAME:
            return self.resume.personal.last_name

        elif semantic_type == FieldSemanticType.FULL_NAME:
            first = self.resume.personal.first_name or ""
            last = self.resume.personal.last_name or ""
            full = f"{first} {last}".strip()
            return full if full else None

        # Location
        elif semantic_type == FieldSemanticType.LOCATION:
            city = self.resume.personal.city or ""
            state = self.resume.personal.state or ""
            country = self.resume.personal.country or ""
            parts = [p for p in [city, state, country] if p]
            return ", ".join(parts) if parts else self.resume.personal.address

        elif semantic_type == FieldSemanticType.ADDRESS_LINE_1:
            return self.resume.personal.address

        elif semantic_type == FieldSemanticType.CITY:
            return self.resume.personal.city

        elif semantic_type == FieldSemanticType.STATE:
            return self.resume.personal.state

        elif semantic_type == FieldSemanticType.COUNTRY:
            return self.resume.personal.country

        elif semantic_type == FieldSemanticType.ZIP_CODE:
            return self.resume.personal.zip_code

        # Professional Links
        elif semantic_type == FieldSemanticType.LINKEDIN_URL:
            return self.resume.personal.linkedin

        elif semantic_type == FieldSemanticType.GITHUB_URL:
            return self.resume.personal.github

        elif semantic_type == FieldSemanticType.PORTFOLIO_URL:
            return self.resume.personal.portfolio

        elif semantic_type == FieldSemanticType.WEBSITE_URL:
            return self.resume.personal.website

        # Work Authorization
        elif semantic_type == FieldSemanticType.WORK_AUTHORIZED:
            return "Yes" if self.resume.work_authorization.authorized_to_work else "No"

        elif semantic_type == FieldSemanticType.REQUIRE_SPONSORSHIP:
            return "Yes" if self.resume.work_authorization.require_sponsorship else "No"

        elif semantic_type == FieldSemanticType.VISA_STATUS:
            return self.resume.work_authorization.visa_status

        # Education (most recent)
        elif semantic_type == FieldSemanticType.SCHOOL_NAME:
            if self.resume.education:
                return self.resume.education[0].school
            return None

        elif semantic_type == FieldSemanticType.DEGREE_TYPE:
            if self.resume.education:
                return self.resume.education[0].degree
            return None

        elif semantic_type == FieldSemanticType.FIELD_OF_STUDY:
            if self.resume.education:
                return self.resume.education[0].field_of_study
            return None

        elif semantic_type == FieldSemanticType.GRADUATION_YEAR:
            if self.resume.education and self.resume.education[0].graduation_year:
                return str(self.resume.education[0].graduation_year)
            return None

        elif semantic_type == FieldSemanticType.GPA:
            if self.resume.education and self.resume.education[0].gpa:
                return str(self.resume.education[0].gpa)
            return None

        # Experience (most recent)
        elif semantic_type == FieldSemanticType.COMPANY_NAME:
            if self.resume.experience:
                return self.resume.experience[0].company
            return None

        elif semantic_type == FieldSemanticType.JOB_TITLE:
            if self.resume.experience:
                return self.resume.experience[0].title
            return None

        elif semantic_type == FieldSemanticType.CURRENT_ROLE:
            if self.resume.experience:
                return "Yes" if self.resume.experience[0].current else "No"
            return None

        elif semantic_type == FieldSemanticType.YEARS_OF_EXPERIENCE:
            # Calculate from experience list (simplistic)
            if self.resume.experience:
                return str(len(self.resume.experience))
            return None

        # Demographics
        elif semantic_type == FieldSemanticType.GENDER:
            if self.resume.demographics:
                return self.resume.demographics.gender
            return None

        elif semantic_type == FieldSemanticType.PRONOUNS:
            if self.resume.demographics:
                return self.resume.demographics.pronouns
            return None

        elif semantic_type == FieldSemanticType.RACE_ETHNICITY:
            if self.resume.demographics:
                return self.resume.demographics.race
            return None

        elif semantic_type == FieldSemanticType.VETERAN_STATUS:
            if self.resume.demographics:
                return self.resume.demographics.veteran_status
            return None

        elif semantic_type == FieldSemanticType.DISABILITY_STATUS:
            if self.resume.demographics:
                return self.resume.demographics.disability_status
            return None

        # No mapping for this type
        return None

    def get_source(self, semantic_type: FieldSemanticType) -> FieldSource:
        """Determine the source for a semantic type.

        If the value exists in resume, return RESUME_JSON.
        Otherwise, return a default that signals downstream to use other sources.
        """
        value = self.get_value(semantic_type)
        if value:
            return FieldSource.RESUME_JSON
        # Fallback: let memory or LLM provide
        return FieldSource.RULE_BASED


# Label normalization for matching ATS field labels to semantic types
_LABEL_TO_SEMANTIC_TYPE = {
    # Email
    "email": FieldSemanticType.EMAIL,
    "email address": FieldSemanticType.EMAIL,
    "e-mail": FieldSemanticType.EMAIL,
    "your email": FieldSemanticType.EMAIL,
    "email id": FieldSemanticType.EMAIL,

    # Phone
    "phone": FieldSemanticType.PHONE,
    "phone number": FieldSemanticType.PHONE,
    "mobile": FieldSemanticType.PHONE,
    "mobile number": FieldSemanticType.PHONE,
    "contact number": FieldSemanticType.PHONE,
    "telephone": FieldSemanticType.PHONE,

    # Name
    "first name": FieldSemanticType.FIRST_NAME,
    "given name": FieldSemanticType.FIRST_NAME,
    "last name": FieldSemanticType.LAST_NAME,
    "surname": FieldSemanticType.LAST_NAME,
    "family name": FieldSemanticType.LAST_NAME,
    "full name": FieldSemanticType.FULL_NAME,
    "your name": FieldSemanticType.FULL_NAME,
    "name": FieldSemanticType.FULL_NAME,
    "preferred name": FieldSemanticType.PREFERRED_NAME,
    "middle name": FieldSemanticType.MIDDLE_NAME,

    # Location
    "location": FieldSemanticType.LOCATION,
    "current location": FieldSemanticType.LOCATION,
    "your location": FieldSemanticType.LOCATION,
    "where are you located": FieldSemanticType.LOCATION,
    "address": FieldSemanticType.ADDRESS_LINE_1,
    "street address": FieldSemanticType.ADDRESS_LINE_1,
    "address line 1": FieldSemanticType.ADDRESS_LINE_1,
    "address line 2": FieldSemanticType.ADDRESS_LINE_2,
    "city": FieldSemanticType.CITY,
    "state": FieldSemanticType.STATE,
    "province": FieldSemanticType.STATE,
    "country": FieldSemanticType.COUNTRY,
    "zip code": FieldSemanticType.ZIP_CODE,
    "zip": FieldSemanticType.ZIP_CODE,
    "postal code": FieldSemanticType.ZIP_CODE,

    # Professional Links
    "linkedin": FieldSemanticType.LINKEDIN_URL,
    "linkedin url": FieldSemanticType.LINKEDIN_URL,
    "linkedin profile": FieldSemanticType.LINKEDIN_URL,
    "github": FieldSemanticType.GITHUB_URL,
    "github url": FieldSemanticType.GITHUB_URL,
    "github profile": FieldSemanticType.GITHUB_URL,
    "portfolio": FieldSemanticType.PORTFOLIO_URL,
    "portfolio url": FieldSemanticType.PORTFOLIO_URL,
    "website": FieldSemanticType.WEBSITE_URL,
    "personal website": FieldSemanticType.WEBSITE_URL,

    # Work Authorization
    "authorized to work": FieldSemanticType.WORK_AUTHORIZED,
    "work authorization": FieldSemanticType.WORK_AUTHORIZED,
    "legally authorized": FieldSemanticType.WORK_AUTHORIZED,
    "require sponsorship": FieldSemanticType.REQUIRE_SPONSORSHIP,
    "sponsorship required": FieldSemanticType.REQUIRE_SPONSORSHIP,
    "need sponsorship": FieldSemanticType.REQUIRE_SPONSORSHIP,
    "visa sponsorship": FieldSemanticType.REQUIRE_SPONSORSHIP,
    "visa status": FieldSemanticType.VISA_STATUS,
    "citizenship": FieldSemanticType.CITIZENSHIP,

    # Education
    "school": FieldSemanticType.SCHOOL_NAME,
    "university": FieldSemanticType.SCHOOL_NAME,
    "college": FieldSemanticType.SCHOOL_NAME,
    "school name": FieldSemanticType.SCHOOL_NAME,
    "university name": FieldSemanticType.SCHOOL_NAME,
    "degree": FieldSemanticType.DEGREE_TYPE,
    "degree type": FieldSemanticType.DEGREE_TYPE,
    "level of education": FieldSemanticType.DEGREE_TYPE,
    "field of study": FieldSemanticType.FIELD_OF_STUDY,
    "major": FieldSemanticType.FIELD_OF_STUDY,
    "graduation year": FieldSemanticType.GRADUATION_YEAR,
    "graduation date": FieldSemanticType.GRADUATION_DATE,
    "gpa": FieldSemanticType.GPA,

    # Experience
    "company": FieldSemanticType.COMPANY_NAME,
    "company name": FieldSemanticType.COMPANY_NAME,
    "employer": FieldSemanticType.COMPANY_NAME,
    "job title": FieldSemanticType.JOB_TITLE,
    "title": FieldSemanticType.JOB_TITLE,
    "position": FieldSemanticType.JOB_TITLE,
    "years of experience": FieldSemanticType.YEARS_OF_EXPERIENCE,
    "years experience": FieldSemanticType.YEARS_OF_EXPERIENCE,
    "total experience": FieldSemanticType.YEARS_OF_EXPERIENCE,

    # Application-Specific
    "how did you hear about us": FieldSemanticType.HEAR_ABOUT_US,
    "referral source": FieldSemanticType.REFERRAL_SOURCE,
    "source": FieldSemanticType.REFERRAL_SOURCE,
    "why are you interested": FieldSemanticType.WHY_INTERESTED,
    "why do you want to work here": FieldSemanticType.WHY_INTERESTED,
    "salary expectation": FieldSemanticType.SALARY_EXPECTATION,
    "expected salary": FieldSemanticType.SALARY_EXPECTATION,
    "start date": FieldSemanticType.EARLIEST_START_DATE,
    "earliest start date": FieldSemanticType.EARLIEST_START_DATE,
    "willing to relocate": FieldSemanticType.WILLING_TO_RELOCATE,
}


def infer_semantic_type(label: str) -> FieldSemanticType:
    """Infer semantic type from an ATS field label.

    Args:
        label: Raw label from ATS (e.g., "Email Address *")

    Returns:
        FieldSemanticType (defaults to UNKNOWN if no match)
    """
    # Normalize: lowercase, strip punctuation
    normalized = label.lower().strip()
    normalized = normalized.replace("*", "").replace(":", "").strip()

    # Direct lookup
    if normalized in _LABEL_TO_SEMANTIC_TYPE:
        return _LABEL_TO_SEMANTIC_TYPE[normalized]

    # Substring matching (e.g., "Email Address (required)" → email)
    for key, semantic_type in _LABEL_TO_SEMANTIC_TYPE.items():
        if key in normalized:
            return semantic_type

    # Fallback
    return FieldSemanticType.UNKNOWN
