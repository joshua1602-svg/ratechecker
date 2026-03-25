import React from "react";

interface AssessmentResult {
  voa_rv?: number;
  modelled_rv_low?: number;
  modelled_rv_high?: number;
  [key: string]: unknown;
}

interface ResultsProps {
  assessmentResult?: AssessmentResult;
  signal?: string;
}

export default function Results({ assessmentResult, signal }: ResultsProps) {
  const voaRv = assessmentResult?.voa_rv ?? 0;
  const modelledLow = assessmentResult?.modelled_rv_low ?? 0;
  const modelledHigh = assessmentResult?.modelled_rv_high ?? 0;

  let verdictHeading: string;
  let verdictBody: string;

  if (voaRv > modelledHigh) {
    verdictHeading = "Your property may be over-assessed";
    verdictBody =
      "Your current rateable value appears higher than similar properties nearby. This may support a review or challenge.";
  } else if (voaRv < modelledLow) {
    verdictHeading = "Your property does not appear over-assessed";
    verdictBody =
      "Your current rateable value appears lower than the level indicated by comparable properties. This is unlikely to support a challenge for reduction.";
  } else {
    verdictHeading = "Your rates appear broadly in line";
    verdictBody =
      "Your current rateable value appears broadly consistent with similar properties nearby on the available evidence.";
  }

  return (
    <div className="results-page">
      <div className="verdict-card">
        {signal && (
          <div className="verdict-badge">
            Overassessment likelihood: {signal}
          </div>
        )}
        <h2 className="verdict-heading">{verdictHeading}</h2>
        <p className="verdict-body">{verdictBody}</p>
        <p className="verdict-disclaimer">
          This is an initial indication based on available data — not a formal
          valuation.
        </p>
      </div>
    </div>
  );
}
