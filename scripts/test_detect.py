from app.ingestion.rfp_extract import _detect_columns, _classify_priority

header = ['Reference', 'Requirement Type', 'Requirement Description', 'Priority', 'Meets Requirement (Yes/No/Partial)', 'Vendor Evidence / Notes']
col_map = _detect_columns(header)
print('Column mapping:', col_map)
print()
for p in ['M', 'S', 'm', 's', 'mandatory', 'should']:
    print(f'  Priority "{p}" -> {_classify_priority(p)}')
