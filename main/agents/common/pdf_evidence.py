"""Join a company's exact CSV row to role-specific PDF retrieval."""
from main.agents.common.evidence import as_evidence
from main.agents.common.csv_judgment import find_unique_company_record
from main.rag.company_pdf import default_pdf_rag


def collect_company_evidence(rag, company, role, *, pdf_rag=None):
    record = find_unique_company_record(rag, company)
    if record is None:
        raise ValueError(f"Company must uniquely match the CSV: {company}")
    retriever = pdf_rag if pdf_rag is not None else default_pdf_rag()
    documents, trace = retriever.retrieve(record.company_id, record.company_name, role)
    return [as_evidence(record), *documents], trace
