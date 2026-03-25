import { useLocation, useNavigate, Navigate } from "react-router-dom";
import BrandMark from "@/components/BrandMark";
import ProductCard from "@/components/ProductCard";
const signalConfig: Record<string, { border: string; heading: string }> = {
  High: { border: "border-l-signal-high", heading: "There may be a case for overassessment" },
  Medium: { border: "border-l-signal-medium", heading: "Your rates may be worth reviewing" },
  Low: { border: "border-l-signal-low", heading: "Your rates appear broadly in line" },
  "Insufficient Data": { border: "border-l-signal-insufficient", heading: "We couldn't find enough comparable data" },
};
const Results = () => {
  const location = useLocation();
  const navigate = useNavigate();
  const state = location.state as { assessRequest?: any; assessmentResult: any; freeFormData: any; ratedComps?: any[] } | null;
  if (!state) return <Navigate to="/" replace />;
  const { assessRequest, assessmentResult, freeFormData, ratedComps = [] } = state;
  const signal = assessmentResult?.signal || "Low";
  const config = signalConfig[signal] || signalConfig.Low;

  const voaRv = assessmentResult?.voa_rv ?? 0;
  const modelledLow = assessmentResult?.modelled_rv_low ?? 0;
  const modelledHigh = assessmentResult?.modelled_rv_high ?? 0;

  const verdictHeading =
    voaRv > modelledHigh
      ? "Your property may be over-assessed"
      : voaRv < modelledLow
      ? "Your property does not appear over-assessed"
      : "Your rates appear broadly in line";

  const verdictBody =
    voaRv > modelledHigh
      ? "Your current rateable value appears higher than similar properties nearby. This may support a review or challenge."
      : voaRv < modelledLow
      ? "Your current rateable value appears lower than the level indicated by comparable properties. This is unlikely to support a challenge for reduction."
      : "Your current rateable value appears broadly consistent with similar properties nearby on the available evidence.";

  return (
    <div className="min-h-screen bg-primary">
      <div className="mx-auto max-w-form px-5 py-8 animate-fade-in">
        <header className="mb-10 flex justify-center"><BrandMark /></header>
        {/* Verdict Card */}
        <div className="rounded-lg border-2 border-accent bg-primary p-6">
          <span className="mb-3 inline-block rounded-sm bg-secondary px-2.5 py-1 text-xs font-semibold uppercase tracking-wider text-secondary-foreground">
            Overassessment likelihood: {signal}
          </span>
          <h2 className="mt-2 text-2xl font-bold text-card-foreground">{verdictHeading}</h2>
          <p className="mt-3 text-sm leading-relaxed text-muted-foreground">
            {verdictBody}
          </p>
          <p className="mt-2 text-xs text-muted-foreground">
            This is an initial indication based on available data — not a formal valuation.
          </p>
        </div>
        {/* Layout indicator */}
        {assessmentResult?.layout_adjustment_applied === true && (
          <p className="mt-3 flex items-center gap-1.5 text-xs text-muted-foreground">
            <span className="inline-block h-1.5 w-1.5 rounded-full bg-accent" />
            Estimate refined using layout data
          </p>
        )}
        {assessmentResult?.layout_adjustment_applied === false && (
          <button
            type="button"
            onClick={() => navigate("/intake?product=report", { state: { assessRequest, assessmentResult, freeFormData, ratedComps } })}
            className="mt-3 text-xs text-accent hover:underline"
          >
            Add layout details to refine your estimate →
          </button>
        )}
        {/* Product Options */}
        <h2 className="mt-12 text-2xl font-bold text-foreground">
          Next step: prepare your challenge
        </h2>
        <div className="mt-6 grid gap-5 sm:grid-cols-2 items-start">
          {/* Rates Assessment — invisible spacer matches Evidence Pack banner */}
          <div className="flex flex-col">
            <div className="rounded-t-lg px-3 py-2 text-center text-xs font-semibold leading-tight invisible" aria-hidden="true">
              £99 credited if you start with the Rates Assessment
            </div>
            <ProductCard
              badge="START HERE"
              title="Rates Assessment"
              price="£99"
              description="See if it's worth challenging your rates."
              features={[
                "Estimated fair rateable value",
                "Potential annual saving",
                "Snapshot of comparable evidence",
              ]}
              subtext="Start here to understand your opportunity."
              ctaLabel="See my estimated saving →"
              variant="accent"
              onClick={() => navigate("/intake?product=report", { state: { assessRequest, assessmentResult, freeFormData, ratedComps } })}
            />
          </div>
          {/* Evidence Pack — banner flush on top of card */}
          <div className="flex flex-col">
            <div className="rounded-t-lg bg-accent px-3 py-2 text-center text-xs font-semibold text-accent-foreground leading-tight">
              £99 credited if you start with the Rates Assessment
            </div>
            <ProductCard
              badge="READY TO CHALLENGE"
              title="Evidence Pack"
              price="£249"
              description="Everything you need to prepare and submit a challenge."
              features={[
                "Full comparable evidence",
                "Adjustment analysis",
                "Pre-written challenge submission",
              ]}
              subtext="Designed to support a Check & Challenge (no guarantee of outcome)."
              ctaLabel="Start my challenge →"
              variant="accent"
              className="rounded-t-none"
              onClick={() => navigate("/intake?product=evidence", { state: { assessRequest, assessmentResult, freeFormData, ratedComps } })}
            />
          </div>
        </div>
        {/* Value reinforcement */}
        <div className="mt-6 rounded-md border border-accent/30 bg-accent/10 px-4 py-4 text-sm text-foreground space-y-3">
          <div className="flex items-start gap-2 text-justify">
            <svg className="mt-0.5 h-4 w-4 shrink-0 text-accent" viewBox="0 0 20 20" fill="currentColor">
              <path fillRule="evenodd" d="M16.704 4.153a.75.75 0 01.143 1.052l-8 10.5a.75.75 0 01-1.127.075l-4.5-4.5a.75.75 0 011.06-1.06l3.894 3.893 7.48-9.817a.75.75 0 011.05-.143z" clipRule="evenodd" />
            </svg>
            <span>If successful, a challenge may result in ongoing annual savings and potential backdated refunds.</span>
          </div>
          <div className="flex items-start gap-2 text-justify">
            <svg className="mt-0.5 h-4 w-4 shrink-0 text-accent" viewBox="0 0 20 20" fill="currentColor">
              <path fillRule="evenodd" d="M16.704 4.153a.75.75 0 01.143 1.052l-8 10.5a.75.75 0 01-1.127.075l-4.5-4.5a.75.75 0 011.06-1.06l3.894 3.893 7.48-9.817a.75.75 0 011.05-.143z" clipRule="evenodd" />
            </svg>
            <span>Agents charge &gt;30% of your saving annually. Our pack gives you the information you need to submit a Challenge yourself.</span>
          </div>
        </div>
        {/* Trust Footer */}
        <div className="mt-8 flex flex-wrap justify-center gap-8 border-t border-border pt-8 text-xs text-muted-foreground">
          <span className="flex items-center gap-1.5">🔒 Secure payment via Stripe</span>
          <span className="flex items-center gap-1.5">📄 PDF delivered within minutes</span>
          <span className="flex items-center gap-1.5">✓ Based on VOA published data</span>
        </div>
        {/* Liability disclaimer */}
        <p className="mt-4 text-center text-[10px] leading-relaxed text-muted-foreground">
          This analysis is based on publicly available data and comparable property estimates. It does not constitute a formal valuation or guarantee a successful outcome.
        </p>
      </div>
    </div>
  );
};
export default Results;
