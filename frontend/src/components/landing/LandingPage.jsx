import {
  ArrowRight,
  Building2,
  CheckCircle2,
  ClipboardCheck,
  Cpu,
  FileStack,
  GitBranch,
  Landmark,
  Scale,
  ShieldCheck,
  TrendingUp,
  Users,
  Vault,
  XCircle,
} from "lucide-react";

const NAV_LINKS = [
  { label: "Features", href: "#problem-solution" },
  { label: "Architecture", href: "#architecture" },
  { label: "Pricing / Intermediaries", href: "#intermediaries" },
  { label: "Documentation", href: "#documentation" },
];

const PIPELINE_STAGES = [
  {
    icon: FileStack,
    title: "Ingestion",
    detail: "SEBI circular PDFs (including scanned/OCR-fallback documents) are pulled and layout-aware parsed within minutes of publication.",
  },
  {
    icon: Scale,
    title: "Domain NLP",
    detail: "Regulatory-tuned language models extract clauses, obligations, and cross-references from the parsed circular text.",
  },
  {
    icon: GitBranch,
    title: "Policy Graph Compiler",
    detail: "Extracted clauses are compiled into a versioned graph of executable OPA/Rego policies, hashed back to their source text.",
  },
  {
    icon: ClipboardCheck,
    title: "HITL Review",
    detail: "Ambiguous or high-impact compiled rules are routed to a compliance officer for sign-off before they ever go live.",
  },
  {
    icon: Cpu,
    title: "Execution Engine",
    detail: "Approved policies evaluate live broker transactions in real time, returning an allow/deny decision with full reasoning.",
  },
  {
    icon: Vault,
    title: "Evidence Vault",
    detail: "Every decision is chained into a tamper-evident audit ledger, hash-linked back to the exact clause that produced it.",
  },
];

const COMPARISON_ROWS = [
  { label: "Time to enforce a new circular", manual: "3–7 days of manual legal review", regengine: "Under 10 minutes, end to end" },
  { label: "Consistency across desks", manual: "Varies by reviewer, error-prone", regengine: "One compiled policy, applied uniformly" },
  { label: "Audit trail", manual: "Scattered emails and spreadsheets", regengine: "Hash-chained, tamper-evident ledger" },
  { label: "Traceability to source clause", manual: "Manually cross-referenced, if at all", regengine: "Every decision hash-linked to source text" },
];

const INTERMEDIARIES = [
  { icon: TrendingUp, title: "Stockbrokers", detail: "Real-time order and margin compliance checks against the latest SEBI circulars, enforced at the point of execution." },
  { icon: Landmark, title: "AMCs", detail: "Fund-level investment restriction and disclosure obligations compiled directly from regulatory text into policy." },
  { icon: Users, title: "Investment Advisers", detail: "Suitability and disclosure requirements tracked and enforced across every client recommendation." },
  { icon: Building2, title: "MIIs", detail: "Market infrastructure institutions get a shared, verifiable source of truth for surveillance and reporting rules." },
];

function Header({ onOpenAuth }) {
  return (
    <header className="sticky top-0 z-40 border-b border-slate-200 bg-white/90 backdrop-blur">
      <div className="mx-auto flex max-w-7xl items-center justify-between px-6 py-4">
        <div className="flex items-center gap-2">
          <ShieldCheck className="h-6 w-6 text-blue-800" />
          <span className="text-base font-semibold tracking-tight text-slate-900">RegEngine AI</span>
        </div>
        <nav className="hidden items-center gap-8 md:flex">
          {NAV_LINKS.map((link) => (
            <a
              key={link.label}
              href={link.href}
              className="text-sm font-medium text-slate-600 transition-colors hover:text-slate-900"
            >
              {link.label}
            </a>
          ))}
        </nav>
        <div className="flex items-center gap-3">
          <button
            onClick={() => onOpenAuth("login")}
            className="rounded-md px-3.5 py-2 text-sm font-medium text-slate-700 transition-colors hover:bg-slate-100"
          >
            Log In
          </button>
          <button
            onClick={() => onOpenAuth("signup")}
            className="rounded-md bg-blue-800 px-4 py-2 text-sm font-semibold text-white shadow-sm transition-colors hover:bg-blue-900"
          >
            Get Started
          </button>
        </div>
      </div>
    </header>
  );
}

function Hero({ onOpenAuth }) {
  return (
    <section className="border-b border-slate-200 bg-gradient-to-b from-slate-50 to-white">
      <div className="mx-auto grid max-w-7xl items-center gap-12 px-6 py-20 md:grid-cols-2 md:py-28">
        <div>
          <div className="mb-5 inline-flex items-center gap-1.5 rounded-full border border-emerald-200 bg-emerald-50 px-3 py-1 text-xs font-semibold text-emerald-700">
            <CheckCircle2 className="h-3.5 w-3.5" />
            Built for SEBI-regulated intermediaries
          </div>
          <h1 className="text-4xl font-bold leading-tight tracking-tight text-slate-900 md:text-5xl">
            Translate SEBI Regulatory Text into{" "}
            <span className="text-blue-800">Machine-Actionable Execution</span>
          </h1>
          <p className="mt-5 max-w-xl text-lg leading-relaxed text-slate-600">
            RegEngine AI ingests SEBI circulars, compiles them into versioned, hash-traceable policy,
            and enforces them against live transactions — with a human sign-off gate and a tamper-evident
            audit trail at every step.
          </p>
          <div className="mt-8 flex flex-wrap items-center gap-3">
            <button
              onClick={() => onOpenAuth("signup")}
              className="flex items-center gap-2 rounded-md bg-blue-800 px-5 py-3 text-sm font-semibold text-white shadow-sm transition-colors hover:bg-blue-900"
            >
              Launch Demo Engine
              <ArrowRight className="h-4 w-4" />
            </button>
            <a
              href="#architecture"
              className="rounded-md border border-slate-300 bg-white px-5 py-3 text-sm font-semibold text-slate-700 transition-colors hover:bg-slate-50"
            >
              Read Documentation
            </a>
          </div>
        </div>

        <div className="rounded-xl border border-slate-200 bg-white p-6 shadow-lg">
          <div className="mb-4 flex items-center justify-between">
            <span className="text-xs font-semibold uppercase tracking-wide text-slate-500">Live Policy Evaluation</span>
            <span className="inline-flex items-center gap-1.5 rounded-full bg-emerald-50 px-2 py-0.5 text-2xs font-semibold text-emerald-700">
              <span className="h-1.5 w-1.5 rounded-full bg-emerald-500" /> Allow
            </span>
          </div>
          <div className="space-y-2 rounded-lg border border-slate-200 bg-slate-50 p-4 font-mono text-xs text-slate-700">
            <p><span className="text-slate-400">circular</span> "SEBI/HO/MIRSD/2025-14"</p>
            <p><span className="text-slate-400">clause</span> "3.2.1 — Margin Disclosure"</p>
            <p><span className="text-slate-400">decision</span> <span className="font-semibold text-emerald-700">allow</span></p>
            <p><span className="text-slate-400">evidence_hash</span> a13f...9c02</p>
          </div>
          <div className="mt-4 grid grid-cols-3 gap-3 text-center">
            <div className="rounded-lg border border-slate-200 p-3">
              <p className="text-lg font-bold text-slate-900">&lt;10min</p>
              <p className="text-2xs text-slate-500">Circular → policy</p>
            </div>
            <div className="rounded-lg border border-slate-200 p-3">
              <p className="text-lg font-bold text-slate-900">100%</p>
              <p className="text-2xs text-slate-500">Clause traceability</p>
            </div>
            <div className="rounded-lg border border-slate-200 p-3">
              <p className="text-lg font-bold text-slate-900">6</p>
              <p className="text-2xs text-slate-500">Pipeline stages</p>
            </div>
          </div>
        </div>
      </div>
    </section>
  );
}

function ProblemSolution() {
  return (
    <section id="problem-solution" className="border-b border-slate-200 bg-white py-20">
      <div className="mx-auto max-w-5xl px-6">
        <div className="mb-12 text-center">
          <h2 className="text-3xl font-bold tracking-tight text-slate-900">Manual compliance can't keep pace</h2>
          <p className="mt-3 text-slate-600">
            Every day a new circular sits untranslated is a day of unmanaged regulatory risk.
          </p>
        </div>
        <div className="grid gap-6 md:grid-cols-2">
          <div className="rounded-xl border border-slate-200 bg-slate-50 p-6">
            <div className="mb-4 flex items-center gap-2">
              <XCircle className="h-5 w-5 text-red-500" />
              <h3 className="font-semibold text-slate-900">Manual Translation Lag</h3>
            </div>
            <p className="mb-4 text-3xl font-bold text-slate-900">3–7 Days</p>
            <ul className="space-y-2 text-sm text-slate-600">
              <li>Legal teams manually read and interpret each circular.</li>
              <li>Rules are hand-translated into policy, inconsistently.</li>
              <li>Errors surface only after an audit or incident.</li>
            </ul>
          </div>
          <div className="rounded-xl border border-blue-200 bg-blue-50/40 p-6">
            <div className="mb-4 flex items-center gap-2">
              <CheckCircle2 className="h-5 w-5 text-emerald-600" />
              <h3 className="font-semibold text-slate-900">RegEngine AI Velocity</h3>
            </div>
            <p className="mb-4 text-3xl font-bold text-blue-800">&lt; 10 Minutes</p>
            <ul className="space-y-2 text-sm text-slate-600">
              <li>Domain NLP extracts obligations directly from source text.</li>
              <li>Policy compiles automatically, with an HITL sign-off gate.</li>
              <li>Every decision is hash-traceable back to its clause.</li>
            </ul>
          </div>
        </div>

        <div className="mt-10 overflow-hidden rounded-xl border border-slate-200">
          <table className="w-full border-collapse text-sm">
            <thead>
              <tr className="bg-slate-50 text-left text-xs font-semibold uppercase tracking-wide text-slate-500">
                <th className="px-5 py-3">Dimension</th>
                <th className="px-5 py-3">Manual Process</th>
                <th className="px-5 py-3">RegEngine AI</th>
              </tr>
            </thead>
            <tbody>
              {COMPARISON_ROWS.map((row) => (
                <tr key={row.label} className="border-t border-slate-200">
                  <td className="px-5 py-3 font-medium text-slate-800">{row.label}</td>
                  <td className="px-5 py-3 text-slate-500">{row.manual}</td>
                  <td className="px-5 py-3 font-medium text-emerald-700">{row.regengine}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </section>
  );
}

function ArchitecturePipeline() {
  return (
    <section id="architecture" className="border-b border-slate-200 bg-slate-50 py-20">
      <div className="mx-auto max-w-7xl px-6">
        <div className="mb-12 text-center">
          <h2 className="text-3xl font-bold tracking-tight text-slate-900">One pipeline, from circular to enforcement</h2>
          <p className="mt-3 text-slate-600">Six stages, each with its own audit trail — nothing skips human review.</p>
        </div>
        <div className="grid gap-4 md:grid-cols-3 lg:grid-cols-6">
          {PIPELINE_STAGES.map((stage, i) => (
            <div key={stage.title} className="relative flex flex-col rounded-xl border border-slate-200 bg-white p-5 shadow-sm">
              <div className="mb-3 flex h-9 w-9 items-center justify-center rounded-lg bg-blue-50 text-blue-800">
                <stage.icon className="h-4.5 w-4.5" />
              </div>
              <p className="mb-1 text-2xs font-semibold uppercase tracking-wide text-cyan-700">Stage {i + 1}</p>
              <h3 className="mb-1.5 text-sm font-semibold text-slate-900">{stage.title}</h3>
              <p className="text-xs leading-relaxed text-slate-500">{stage.detail}</p>
              {i < PIPELINE_STAGES.length - 1 && (
                <ArrowRight className="absolute -right-3 top-1/2 hidden h-4 w-4 -translate-y-1/2 text-emerald-500 lg:block" />
              )}
            </div>
          ))}
        </div>
      </div>
    </section>
  );
}

function Intermediaries() {
  return (
    <section id="intermediaries" className="border-b border-slate-200 bg-white py-20">
      <div className="mx-auto max-w-7xl px-6">
        <div className="mb-12 text-center">
          <h2 className="text-3xl font-bold tracking-tight text-slate-900">Built for every SEBI-regulated intermediary</h2>
          <p className="mt-3 text-slate-600">One compliance control plane, tailored obligations per entity type.</p>
        </div>
        <div className="grid gap-5 sm:grid-cols-2 lg:grid-cols-4">
          {INTERMEDIARIES.map((item) => (
            <div
              key={item.title}
              className="rounded-xl border border-slate-200 bg-white p-6 shadow-sm transition-shadow hover:shadow-md"
            >
              <div className="mb-4 flex h-10 w-10 items-center justify-center rounded-lg bg-emerald-50 text-emerald-700">
                <item.icon className="h-5 w-5" />
              </div>
              <h3 className="mb-2 font-semibold text-slate-900">{item.title}</h3>
              <p className="text-sm leading-relaxed text-slate-500">{item.detail}</p>
            </div>
          ))}
        </div>
      </div>
    </section>
  );
}

function Footer() {
  return (
    <footer id="documentation" className="bg-slate-50">
      <div className="mx-auto max-w-7xl px-6 py-12">
        <div className="grid gap-10 md:grid-cols-4">
          <div>
            <div className="mb-3 flex items-center gap-2">
              <ShieldCheck className="h-5 w-5 text-blue-800" />
              <span className="font-semibold text-slate-900">RegEngine AI</span>
            </div>
            <p className="text-sm text-slate-500">
              Automated SEBI regulatory compliance and document intelligence.
            </p>
          </div>
          <div>
            <h4 className="mb-3 text-xs font-semibold uppercase tracking-wide text-slate-500">Documentation</h4>
            <ul className="space-y-2 text-sm text-slate-600">
              <li><a href="#architecture" className="hover:text-slate-900">Architecture Overview</a></li>
              <li><a href="#problem-solution" className="hover:text-slate-900">Why RegEngine AI</a></li>
              <li><a href="#intermediaries" className="hover:text-slate-900">Intermediary Coverage</a></li>
            </ul>
          </div>
          <div>
            <h4 className="mb-3 text-xs font-semibold uppercase tracking-wide text-slate-500">Legal</h4>
            <ul className="space-y-2 text-sm text-slate-600">
              <li><a href="#" className="hover:text-slate-900">Privacy Policy</a></li>
              <li><a href="#" className="hover:text-slate-900">Terms of Service</a></li>
            </ul>
          </div>
          <div>
            <h4 className="mb-3 text-xs font-semibold uppercase tracking-wide text-slate-500">Platform Status</h4>
            <div className="flex items-center gap-1.5 text-sm text-emerald-700">
              <span className="h-2 w-2 rounded-full bg-emerald-500" />
              All Systems Operational
            </div>
          </div>
        </div>
        <div className="mt-10 border-t border-slate-200 pt-6 text-xs text-slate-400">
          © {new Date().getFullYear()} RegEngine AI. All rights reserved.
        </div>
      </div>
    </footer>
  );
}

export default function LandingPage({ onOpenAuth }) {
  return (
    <div className="min-h-screen bg-white">
      <Header onOpenAuth={onOpenAuth} />
      <main>
        <Hero onOpenAuth={onOpenAuth} />
        <ProblemSolution />
        <ArchitecturePipeline />
        <Intermediaries />
      </main>
      <Footer />
    </div>
  );
}
