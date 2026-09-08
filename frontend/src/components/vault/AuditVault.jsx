import { Loader2, Pause, Play, ShieldCheck, ShieldX, Wrench } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import Card from "../shared/Card";
import TransactionRow from "./TransactionRow";

const LIVE_INTERVAL_MS = 3500;

export default function AuditVault({ initialFeed = [], onRefreshFeed, onVerifyChain }) {
  const [feed, setFeed] = useState(initialFeed);
  const [isLive, setIsLive] = useState(true);
  const [latestSeq, setLatestSeq] = useState(null);
  const [tamperedSeq, setTamperedSeq] = useState(null);
  const [verifyResult, setVerifyResult] = useState(null);
  const [loading, setLoading] = useState(false);
  const newestRef = useRef(initialFeed[0]);

  useEffect(() => {
    setFeed(initialFeed);
    newestRef.current = initialFeed[0];
  }, [initialFeed]);

  useEffect(() => {
    if (!isLive || !onRefreshFeed) return undefined;
    const interval = setInterval(async () => {
      try {
        const fresh = await onRefreshFeed();
        if (fresh && fresh.length > 0) {
          setFeed(fresh);
          if (fresh[0]?.sequenceNum !== newestRef.current?.sequenceNum) {
            setLatestSeq(fresh[0]?.sequenceNum);
            newestRef.current = fresh[0];
          }
        }
      } catch (e) {
        // ignore poll errors
      }
    }, LIVE_INTERVAL_MS);
    return () => clearInterval(interval);
  }, [isLive, onRefreshFeed]);

  const runVerification = async () => {
    if (onVerifyChain) {
      setLoading(true);
      try {
        const res = await onVerifyChain();
        setVerifyResult({
          valid: res.valid,
          entriesChecked: res.entries_checked ?? res.entriesChecked ?? feed.length,
          breaks: (res.breaks || []).map((b) => ({
            sequenceNum: b.sequence_num ?? b.sequenceNum,
            reason: b.reason || "Hash mismatch",
          })),
        });
      } catch (err) {
        setVerifyResult({
          valid: false,
          entriesChecked: feed.length,
          breaks: [{ sequenceNum: 0, reason: err instanceof Error ? err.message : "Verification request failed" }],
        });
      } finally {
        setLoading(false);
      }
    } else {
      // In-memory pointer validation over visible feed
      const chronological = [...feed].reverse();
      const breaks = [];
      for (let i = 1; i < chronological.length; i++) {
        const entry = chronological[i];
        const prev = chronological[i - 1];
        if (entry.previousHash !== prev.currentHash) {
          breaks.push({
            sequenceNum: entry.sequenceNum,
            reason: "previous_hash does not match the prior row's current_hash",
          });
        }
      }
      setVerifyResult({
        valid: breaks.length === 0,
        entriesChecked: chronological.length,
        breaks,
      });
    }
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
        currentHash: "tampered_" + target.currentHash.slice(9),
      };
      const copy = [...prev];
      copy[targetIndex] = forged;
      return copy;
    });
    setVerifyResult(null);
  };

  return (
    <div className="flex h-full flex-col gap-4">
      <div className="flex items-center gap-2">
        <button
          onClick={() => setIsLive((v) => !v)}
          className="flex items-center gap-1.5 rounded-sm border border-ink-700 bg-ink-900 px-2.5 py-1.5 text-sm font-medium text-slate-700 hover:bg-ink-850"
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
          disabled={loading}
          className="flex items-center gap-1.5 rounded-sm border border-blue-200 bg-blue-100 px-2.5 py-1.5 text-sm font-medium text-blue-800 hover:bg-blue-200 disabled:opacity-50"
        >
          {loading ? (
            <Loader2 className="h-3.5 w-3.5 animate-spin" />
          ) : (
            <ShieldCheck className="h-3.5 w-3.5" />
          )}
          {loading ? "Verifying..." : "Verify chain integrity"}
        </button>
        <button
          onClick={simulateTamper}
          className="ml-auto flex items-center gap-1.5 rounded-sm border border-ink-700 px-2.5 py-1.5 text-sm font-medium text-slate-500 hover:border-red-300 hover:text-red-700"
          title="Demo only: forges a row in place to demonstrate integrity checking."
        >
          <Wrench className="h-3.5 w-3.5" /> Simulate tamper
        </button>
      </div>

      {verifyResult && (
        <Card
          className={`flex items-start gap-3 p-3 ${
            verifyResult.valid
              ? "border-green-200 bg-green-50"
              : "border-red-200 bg-red-50"
          }`}
        >
          {verifyResult.valid ? (
            <ShieldCheck className="mt-0.5 h-5 w-5 shrink-0 text-green-700" />
          ) : (
            <ShieldX className="mt-0.5 h-5 w-5 shrink-0 text-red-700" />
          )}
          <div className="text-sm">
            <p
              className={`font-medium ${verifyResult.valid ? "text-green-800" : "text-red-800"}`}
            >
              {verifyResult.valid
                ? `Chain intact across ${verifyResult.entriesChecked} entries.`
                : `Integrity violation detected across ${verifyResult.entriesChecked} entries.`}
            </p>
            {!verifyResult.valid && (
              <ul className="mt-1 list-inside list-disc text-red-700">
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
          real append-only path) — run &ldquo;Verify chain
          integrity&rdquo; to see it caught.
        </p>
      )}

      <Card className="flex-1 overflow-hidden">
        <div className="h-full overflow-y-auto scrollbar-thin">
          <table className="w-full min-w-[900px] border-collapse">
            <thead className="sticky top-0 border-b border-ink-700 bg-ink-850 text-left text-2xs font-semibold uppercase tracking-wide text-slate-500">
              <tr>
                <th className="px-4 py-2.5">Seq</th>
                <th className="px-4 py-2.5">Transaction</th>
                <th className="px-4 py-2.5">Broker</th>
                <th className="px-4 py-2.5">Evaluated</th>
                <th className="px-4 py-2.5">SEBI Clause Link</th>
                <th className="px-4 py-2.5">Result</th>
                <th className="px-4 py-2.5">Hash Chain</th>
              </tr>
            </thead>
            <tbody>
              {feed.length === 0 ? (
                <tr>
                  <td colSpan={7} className="py-16 text-center text-sm text-slate-400">
                    <ShieldCheck className="mx-auto mb-2 h-7 w-7 text-slate-400" />
                    <p className="font-medium text-slate-700">No ledger transactions recorded yet</p>
                    <p className="mt-0.5 text-xs text-slate-400">
                      Transactions evaluated by the policy engine will appear here with cryptographic hash chains.
                    </p>
                  </td>
                </tr>
              ) : (
                feed.map((entry) => (
                  <TransactionRow
                    key={entry.sequenceNum}
                    entry={entry}
                    isNew={entry.sequenceNum === latestSeq}
                  />
                ))
              )}
            </tbody>
          </table>
        </div>
      </Card>
    </div>
  );
}
