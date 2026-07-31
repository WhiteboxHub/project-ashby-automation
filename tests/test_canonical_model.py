"""Tests for canonical application model.

Verify:
1. Field semantic type inference from labels
2. Validation rules for each type
3. Confidence calculation
4. ApplicationSession field management
5. Adapter conversions
"""

import pytest

from jobcli.canonical.confidence import calculate_confidence, should_request_human_override
from jobcli.canonical.field_builder import FieldBuilder, create_empty_field
from jobcli.canonical.mappers import ResumeFieldMapper, infer_semantic_type
from jobcli.canonical.models import (
    ApplicationField,
    ApplicationSession,
    ConfidenceScore,
    FieldSemanticType,
    FieldSource,
    ValidationResult,
    ValidationSeverity,
)
from jobcli.canonical.validators import validate_field_value
from jobcli.profile.schemas import (
    ATSType,
    PersonalInfo,
    ResumeData,
    WorkAuthorization,
)


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def sample_resume() -> ResumeData:
    """Sample resume for testing."""
    return ResumeData(
        personal=PersonalInfo(
            first_name="Jane",
            last_name="Doe",
            email="jane.doe@example.com",
            phone="+1-555-0100",
            city="San Francisco",
            state="CA",
            country="USA",
            zip_code="94102",
            linkedin="https://linkedin.com/in/janedoe",
            github="https://github.com/janedoe",
        ),
        work_authorization=WorkAuthorization(
            authorized_to_work=True,
            require_sponsorship=False,
        ),
        education=[],
        experience=[],
    )


@pytest.fixture
def field_builder(sample_resume) -> FieldBuilder:
    """FieldBuilder with sample resume."""
    return FieldBuilder(
        resume=sample_resume,
        ats_type=ATSType.GREENHOUSE,
        page_index=0,
    )


# ── Test Semantic Type Inference ──────────────────────────────────────────────


def test_infer_semantic_type_email():
    """Test email field detection from various labels."""
    assert infer_semantic_type("Email") == FieldSemanticType.EMAIL
    assert infer_semantic_type("Email Address *") == FieldSemanticType.EMAIL
    assert infer_semantic_type("E-mail") == FieldSemanticType.EMAIL
    assert infer_semantic_type("Your Email") == FieldSemanticType.EMAIL


def test_infer_semantic_type_phone():
    """Test phone field detection."""
    assert infer_semantic_type("Phone") == FieldSemanticType.PHONE
    assert infer_semantic_type("Phone Number *") == FieldSemanticType.PHONE
    assert infer_semantic_type("Mobile") == FieldSemanticType.PHONE
    assert infer_semantic_type("Contact Number") == FieldSemanticType.PHONE


def test_infer_semantic_type_name():
    """Test name field detection."""
    assert infer_semantic_type("First Name") == FieldSemanticType.FIRST_NAME
    assert infer_semantic_type("Last Name *") == FieldSemanticType.LAST_NAME
    assert infer_semantic_type("Full Name") == FieldSemanticType.FULL_NAME


def test_infer_semantic_type_work_auth():
    """Test work authorization field detection."""
    assert infer_semantic_type("Authorized to work in the US?") == FieldSemanticType.WORK_AUTHORIZED
    assert infer_semantic_type("Require sponsorship?") == FieldSemanticType.REQUIRE_SPONSORSHIP


def test_infer_semantic_type_unknown():
    """Test unknown field returns UNKNOWN."""
    assert infer_semantic_type("Random Custom Field") == FieldSemanticType.UNKNOWN


# ── Test Validation Rules ─────────────────────────────────────────────────────


def test_validate_email_valid():
    """Test valid email passes validation."""
    result = validate_field_value(FieldSemanticType.EMAIL, "jane@example.com", required=False)
    assert result.severity == ValidationSeverity.OK


def test_validate_email_invalid():
    """Test invalid email fails validation."""
    result = validate_field_value(FieldSemanticType.EMAIL, "not-an-email", required=False)
    assert result.severity == ValidationSeverity.ERROR


def test_validate_email_empty_required():
    """Test empty required email fails."""
    result = validate_field_value(FieldSemanticType.EMAIL, None, required=True)
    assert result.severity == ValidationSeverity.ERROR
    assert "empty" in result.message.lower()


def test_validate_email_empty_optional():
    """Test empty optional email passes."""
    result = validate_field_value(FieldSemanticType.EMAIL, None, required=False)
    assert result.severity == ValidationSeverity.OK


def test_validate_phone_valid():
    """Test valid phone formats."""
    assert validate_field_value(FieldSemanticType.PHONE, "+1-555-0100", False).severity == ValidationSeverity.OK
    assert validate_field_value(FieldSemanticType.PHONE, "(555) 123-4567", False).severity == ValidationSeverity.OK
    assert validate_field_value(FieldSemanticType.PHONE, "5551234567", False).severity == ValidationSeverity.OK


def test_validate_url_valid():
    """Test valid URL formats."""
    result = validate_field_value(FieldSemanticType.WEBSITE_URL, "https://example.com", False)
    assert result.severity == ValidationSeverity.OK


def test_validate_linkedin_url_valid():
    """Test valid LinkedIn URL."""
    result = validate_field_value(
        FieldSemanticType.LINKEDIN_URL,
        "https://linkedin.com/in/janedoe",
        False,
    )
    assert result.severity == ValidationSeverity.OK


def test_validate_linkedin_url_suspicious():
    """Test suspicious LinkedIn URL (valid URL but not linkedin.com)."""
    result = validate_field_value(
        FieldSemanticType.LINKEDIN_URL,
        "https://example.com",
        False,
    )
    assert result.severity == ValidationSeverity.WARNING


def test_validate_gpa_valid():
    """Test valid GPA."""
    assert validate_field_value(FieldSemanticType.GPA, "3.5", False).severity == ValidationSeverity.OK
    assert validate_field_value(FieldSemanticType.GPA, "4.0", False).severity == ValidationSeverity.OK


def test_validate_gpa_out_of_range():
    """Test GPA outside normal range."""
    result = validate_field_value(FieldSemanticType.GPA, "6.0", False)
    assert result.severity == ValidationSeverity.WARNING


def test_validate_gpa_invalid():
    """Test non-numeric GPA."""
    result = validate_field_value(FieldSemanticType.GPA, "A+", False)
    assert result.severity == ValidationSeverity.ERROR


# ── Test Confidence Calculation ───────────────────────────────────────────────


def test_confidence_resume_json_valid():
    """Test confidence for resume JSON with valid value."""
    validation = ValidationResult(severity=ValidationSeverity.OK, message="Valid")
    confidence = calculate_confidence(FieldSource.RESUME_JSON, validation)
    assert confidence.value >= 0.9  # High confidence


def test_confidence_llm_reasoning_valid():
    """Test confidence for LLM reasoning with valid value."""
    validation = ValidationResult(severity=ValidationSeverity.OK, message="Valid")
    confidence = calculate_confidence(FieldSource.LLM_REASONING, validation)
    assert 0.6 <= confidence.value < 0.9  # Medium confidence


def test_confidence_validation_error():
    """Test confidence drops when validation fails."""
    validation = ValidationResult(severity=ValidationSeverity.ERROR, message="Invalid")
    confidence = calculate_confidence(FieldSource.RESUME_JSON, validation)
    assert confidence.value < 0.5  # Low confidence even for trusted source


def test_confidence_with_historical_success():
    """Test historical success rate boosts confidence."""
    validation = ValidationResult(severity=ValidationSeverity.OK, message="Valid")
    confidence = calculate_confidence(
        FieldSource.LLM_REASONING,
        validation,
        historical_success_rate=0.9,
    )
    assert confidence.value > 0.55  # Boosted by history


def test_should_request_human_override_required_low_confidence():
    """Test human override requested for required field with low confidence."""
    confidence = ConfidenceScore(
        value=0.5,
        source_weight=0.5,
        validation_passed=True,
        historical_success_rate=None,
    )
    assert should_request_human_override(confidence, required=True) is True


def test_should_request_human_override_optional_medium_confidence():
    """Test human override NOT requested for optional field with medium confidence."""
    confidence = ConfidenceScore(
        value=0.6,
        source_weight=0.6,
        validation_passed=True,
        historical_success_rate=None,
    )
    assert should_request_human_override(confidence, required=False) is False


# ── Test Resume Field Mapper ──────────────────────────────────────────────────


def test_resume_mapper_email(sample_resume):
    """Test mapper extracts email from resume."""
    mapper = ResumeFieldMapper(sample_resume)
    assert mapper.get_value(FieldSemanticType.EMAIL) == "jane.doe@example.com"


def test_resume_mapper_full_name(sample_resume):
    """Test mapper constructs full name."""
    mapper = ResumeFieldMapper(sample_resume)
    assert mapper.get_value(FieldSemanticType.FULL_NAME) == "Jane Doe"


def test_resume_mapper_work_authorized(sample_resume):
    """Test mapper returns work authorization as Yes/No."""
    mapper = ResumeFieldMapper(sample_resume)
    assert mapper.get_value(FieldSemanticType.WORK_AUTHORIZED) == "Yes"
    assert mapper.get_value(FieldSemanticType.REQUIRE_SPONSORSHIP) == "No"


def test_resume_mapper_missing_field(sample_resume):
    """Test mapper returns None for missing fields."""
    mapper = ResumeFieldMapper(sample_resume)
    # No education in sample resume
    assert mapper.get_value(FieldSemanticType.SCHOOL_NAME) is None


# ── Test Field Builder ────────────────────────────────────────────────────────


def test_field_builder_from_label(field_builder):
    """Test building field from label (auto-infers semantic type)."""
    field = field_builder.from_label(
        field_id="email_input",
        raw_label="Email Address *",
        required=True,
        ats_selector="input[name='email']",
    )

    assert field.field_id == "email_input"
    assert field.semantic_type == FieldSemanticType.EMAIL
    assert field.value == "jane.doe@example.com"  # From resume
    assert field.source == FieldSource.RESUME_JSON
    assert field.validation.severity == ValidationSeverity.OK
    assert field.confidence.value > 0.9


def test_field_builder_unknown_label(field_builder):
    """Test building field with unknown label."""
    field = field_builder.from_label(
        field_id="custom_field",
        raw_label="Custom Question",
        required=False,
        ats_selector="input[name='custom']",
    )

    assert field.semantic_type == FieldSemanticType.UNKNOWN
    # No resume mapping, so DEFAULT_VALUE
    assert field.source == FieldSource.DEFAULT_VALUE


def test_field_builder_override_value(field_builder):
    """Test building field with override value (from memory/LLM)."""
    field = field_builder.from_semantic_type(
        field_id="email_override",
        semantic_type=FieldSemanticType.EMAIL,
        raw_label="Email",
        required=True,
        override_value="override@example.com",
        override_source=FieldSource.MEMORY_HIGH_CONFIDENCE,
    )

    assert field.value == "override@example.com"
    assert field.source == FieldSource.MEMORY_HIGH_CONFIDENCE


def test_field_builder_update_value(field_builder):
    """Test updating field value after construction."""
    field = field_builder.from_label(
        field_id="email_input",
        raw_label="Email",
        required=True,
    )

    # Update with human-provided value
    updated = field_builder.update_field_value(
        field=field,
        new_value="new@example.com",
        new_source=FieldSource.HUMAN_PROVIDED,
        historical_success_rate=0.95,
    )

    assert updated.value == "new@example.com"
    assert updated.source == FieldSource.HUMAN_PROVIDED
    assert updated.confidence.value > 0.8  # High confidence for human provided


# ── Test Application Session ──────────────────────────────────────────────────


def test_application_session_add_field():
    """Test adding fields to session."""
    session = ApplicationSession(
        session_id="test-session",
        job_id=123,
        ats_type=ATSType.GREENHOUSE,
        job_url="https://example.com/job",
    )

    field = create_empty_field(
        field_id="email",
        raw_label="Email",
        required=True,
        ats_type=ATSType.GREENHOUSE,
    )

    session.add_field(field)
    assert len(session.fields) == 1
    assert session.get_field("email") == field


def test_application_session_dedupe_fields():
    """Test fields are deduped by field_id."""
    session = ApplicationSession(
        session_id="test-session",
        job_id=123,
        ats_type=ATSType.GREENHOUSE,
        job_url="https://example.com/job",
    )

    field1 = create_empty_field("email", "Email", True, ATSType.GREENHOUSE)
    field2 = create_empty_field("email", "Email Updated", True, ATSType.GREENHOUSE)

    session.add_field(field1)
    session.add_field(field2)

    assert len(session.fields) == 1
    assert session.get_field("email").raw_label == "Email Updated"


def test_application_session_required_empty_fields():
    """Test getting required empty fields."""
    session = ApplicationSession(
        session_id="test-session",
        job_id=123,
        ats_type=ATSType.GREENHOUSE,
        job_url="https://example.com/job",
    )

    field1 = create_empty_field("email", "Email", True, ATSType.GREENHOUSE)
    field1 = field1.model_copy(update={"value": None})  # Empty

    field2 = create_empty_field("phone", "Phone", False, ATSType.GREENHOUSE)
    field2 = field2.model_copy(update={"value": None})  # Empty but optional

    field3 = create_empty_field("name", "Name", True, ATSType.GREENHOUSE)
    field3 = field3.model_copy(update={"value": "Jane"})  # Filled

    session.add_field(field1)
    session.add_field(field2)
    session.add_field(field3)

    empty_required = session.get_required_empty_fields()
    assert len(empty_required) == 1
    assert empty_required[0].field_id == "email"


def test_application_session_mark_completed():
    """Test marking fields as completed."""
    session = ApplicationSession(
        session_id="test-session",
        job_id=123,
        ats_type=ATSType.GREENHOUSE,
        job_url="https://example.com/job",
    )

    session.mark_completed("email")
    assert "email" in session.completed_field_ids


def test_application_session_snapshot():
    """Test creating session snapshots."""
    session = ApplicationSession(
        session_id="test-session",
        job_id=123,
        ats_type=ATSType.GREENHOUSE,
        job_url="https://example.com/job",
    )

    field = create_empty_field("email", "Email", True, ATSType.GREENHOUSE)
    session.add_field(field)

    snapshot = session.snapshot()

    assert "timestamp" in snapshot
    assert snapshot["fields_total"] == 1
    assert len(snapshot["fields"]) == 1
    assert len(session.snapshots) == 1


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
