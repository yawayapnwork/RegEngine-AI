import { Pause, Play, ShieldCheck, ShieldX, Wrench } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import Card from "../shared/Card";
import { generateNextEntry, verifyFeed } from "./mockChain";
import TransactionRow from "./TransactionRow";

const LIVE_INTERVAL_MS = 3500;
const MAX_FEED_LENGTH = 40;

export default function AuditVault({ initialFeed }) {
  const [feed, setFeed] = useState(initialFeed);
  const [isLive, setIsLive] = useState(true);
  const [latestSeq, setLatestSeq] = useState(null);
  const [tamperedSeq, setTamperedSeq] = useState(null);
  const [verifyResult, setVerifyResult] = useState(null);
  const newestRef = useRef(initialFeed[0]);

  useEffect(() => {
    if (!isLive) return undefined;
    const interval = setInterval(() => {
      const next = generateNextEntry(newestRef.current);
      newestRef.current = next;
      setFeed((prev) => [next, ...prev].slice(0, MAX_FEED_LENGTH));
      setLatestSeq(next.sequenceNum);
      setVerifyResult(null);
    }, LIVE_INTERVAL_MS);
    return () => clearInterval(interval);
  }, [isLive]);

  const runVerification = () => {
    setVerifyResult(verifyFeed(feed));
  };

  const simulateTamper = () => {
    setFeed((prev) => {
      if (prev.length < 2) return prev;
      const targetIndex = Math.floor(prev.length / 2);
      const target = prev[targetIndex];
      setTamperedSeq(target.sequenceNum);
      const forged = {
        ...target,
        evaluationResult: target.evaluationResult === "PASS" ? "FAIL" : "PASS",
      };
      const copy = [...prev];
      copy[targetIndex] = forged;
      return copy;
    });
    setVerifyResult(null);
  };

  return (
    <div className="flex h-full flex-col gap-4">
      <div className="flex items-center gap-3">
        <button
          onClick={() => setIsLive((v) => !v)}
          className="flex items-center gap-1.5 rounded-lg border border-ink-700 bg-ink-900 px-3 py-1.5 text-sm font-medium text-slate-300 hover:bg-ink-800"
        >
          {isLive ? (
            <Pause className="h-3.5 w-3.5" />
          ) : (
            <Play className="h-3.5 w-3.5" />
          )}
          {isLive ? "Pause live feed" : "Resume live feed"}
        </button>
        <button
          onClick={runVerification}
          className="flex items-center gap-1.5 rounded-lg bg-sky-500/15 px-3 py-1.5 text-sm font-medium text-sky-400 ring-1 ring-inset ring-sky-500/30 hover:bg-sky-500/25"
        >
          <ShieldCheck className="h-3.5 w-3.5" /> Verify chain integrity
        </button>
        <button
          onClick={simulateTamper}
          className="ml-auto flex items-center gap-1.5 rounded-lg border border-ink-700 px-3 py-1.5 text-sm font-medium text-slate-500 hover:border-rose-500/40 hover:text-rose-400"
          title="Demo only: forges a row in place to show verify_chain catching it, mirroring app/ledger/verifier.py."
        >
          <Wrench className="h-3.5 w-3.5" /> Simulate tamper
        </button>
      </div>

      {verifyResult && (
        <Card
          className={`flex items-start gap-3 p-4 ${
            verifyResult.valid
              ? "border-emerald-500/30 bg-emerald-500/5"
              : "border-rose-500/30 bg-rose-500/5"
          }`}
        >
          {verifyResult.valid ? (
            <ShieldCheck className="mt-0.5 h-5 w-5 shrink-0 text-emerald-400" />
          ) : (
            <ShieldX className="mt-0.5 h-5 w-5 shrink-0 text-rose-400" />
          )}
          <div className="text-sm">
            <p
              className={`font-medium ${verifyResult.valid ? "text-emerald-400" : "text-rose-400"}`}
            >
              {verifyResult.valid
                ? `Chain intact across ${verifyResult.entriesChecked} entries.`
                : `Integrity violation detected across ${verifyResult.entriesChecked} entries.`}
            </p>
            {!verifyResult.valid && (
              <ul className="mt-1 list-inside list-disc text-rose-300/80">
                {verifyResult.breaks.map((b, i) => (
                  <li key={i}>
                    sequence #{b.sequenceNum}: {b.reason}
                  </li>
                ))}
              </ul>
            )}
          </div>
        </Card>
      )}

      {tamperedSeq && (
        <p className="text-xs text-slate-500">
          Row #{tamperedSeq} was forged in place for this demo (bypassing the
          real append-only path) &mdash; run &ldquo;Verify chain
          integrity&rdquo; to see it caught.
        </p>
      )}

      <Card className="flex-1 overflow-hidden">
        <div className="h-full overflow-y-auto scrollbar-thin">
          <table className="w-full border-collapse">
            <thead className="sticky top-0 bg-ink-900/95 text-left text-xs uppercase tracking-wide text-slate-500 backdrop-blur">
              <tr>
                <th className="px-4 py-3 font-medium">Seq</th>
                <th className="px-4 py-3 font-medium">Transaction</th>
                <th className="px-4 py-3 font-medium">Broker</th>
                <th className="px-4 py-3 font-medium">Evaluated</th>
                <th className="px-4 py-3 font-medium">SEBI Clause Link</th>
                <th className="px-4 py-3 font-medium">Result</th>
                <th className="px-4 py-3 font-medium">Hash Chain</th>
              </tr>
            </thead>
            <tbody>
              {feed.map((entry) => (
                <TransactionRow
                  key={entry.sequenceNum}
                  entry={entry}
                  isNew={entry.sequenceNum === latestSeq}
                />
              ))}
            </tbody>
          </table>
        </div>
      </Card>
    </div>
  );
}
