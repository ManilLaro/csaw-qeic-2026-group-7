"""Edit this file. Return partial evidence when no stronger claim is supported."""
def empty_submission():
    return dict(verdict='INSUFFICIENT_EVIDENCE', layer_assessments={}, suspected_fault_type=None,
                affected_logical_operations=[], expected_physical_resources=[], suspected_realized_resources=[],
                artifact_evidence=[], diagnostic_evidence=[], estimated_parameters={}, incidental_anomalies=[],
                root_cause=None, proposed_repair=None, repair_validation=None, confidence=0.0)
def solve_stage1(stage_dir):
    return empty_submission()
def solve_stage2(stage_dir):
    return empty_submission()
def solve(stage_name, stage_dir):
    return {'stage_1':solve_stage1, 'stage_2':solve_stage2}[stage_name](stage_dir)
