"""Rule-based analyzer wrapping the conservative description classifier."""
from src.analyze.classifier import classify
from src.core.models import Analysis


class RulesAnalyzer:
    name = "rules"
    version = "1"

    def analyze(self, event):
        finding = classify(event.payload, detected_change=True)
        if finding["important"]:
            return Analysis("high", "court_order_candidate", reasons=tuple(finding["reasons"]))
        return Analysis("low", "other", reasons=("No court-origin wording at the start of the description",))
