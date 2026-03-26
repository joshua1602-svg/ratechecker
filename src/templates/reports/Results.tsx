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

// Four-tier verdict logic
// Tier 1 — Undervalued:     voaRv < modelledLow
// Tier 2 — Broadly inline:  voaRv within modelled band (±5%)
// Tier 3 — Slightly over:   voaRv > modelledHigh, overage < 15%
// Tier 4 — Overassessed:    voaRv > modelledHigh, overage >= 15%
function getVerdict(
  voaRv: number,
  modelledLow: number,
  modelledHigh: number
): { heading: string; body: string; tier: "undervalued" | "inline" | "slight" | "over" } {
  if (voaRv <= 0 || modelledHigh <= 0) {
    return {
      tier: "inline",
      heading: "Your rates appear broadly in line",
      body: "Your current rateable value appears broadly consistent with similar properties nearby on the available evidence.",
    };
  }

  if (voaRv < modelledLow) {
    return {
      tier: "undervalued",
      heading: "Your property does not appear over-assessed",
      body: "Your current rateable value appears lower than the level indicated by comparable properties. This is unlikely to support a challenge for reduction.",
    };
  }

  if (voaRv <= modelledHigh) {
    return {
      tier: "inline",
      heading: "Your rates appear broadly in line",
      body: "Your current rateable value appears broadly consistent with similar properties nearby on the available evidence.",
    };
  }

  // voaRv > modelledHigh — split by degree
  const overage = (voaRv - modelledHigh) / modelledHigh;

  if (overage < 0.15) {
    return {
      tier: "slight",
      heading: "Your rates may be slightly high",
      body: "Your current rateable value appears marginally above similar properties nearby. There may be a limited case for review depending on the strength of comparable evidence.",
    };
  }

  return {
    tier: "over",
    heading: "Your rates appear overassessed",
    body: "Your current rateable value is notably above comparable properties nearby. The evidence suggests a reasonable case for challenge.",
  };
}

export default function Results({ assessmentResult, signal }: ResultsProps) {
  const voaRv = assessmentResult?.voa_rv ?? 0;
  const modelledLow = assessmentResult?.modelled_rv_low ?? 0;
  const modelledHigh = assessmentResult?.modelled_rv_high ?? 0;

  const { heading: verdictHeading, body: verdictBody } = getVerdict(
    voaRv,
    modelledLow,
    modelledHigh
  );

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
