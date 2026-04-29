import React, { useEffect, useMemo, useState } from "react";
import { createRoot } from "react-dom/client";
import {
  ArrowDownLeft,
  ArrowLeft,
  ArrowUpRight,
  BookOpen,
  ChevronDown,
  Clock3,
  Play,
  RefreshCcw,
  Send,
  Wallet,
  Zap
} from "lucide-react";
import "./styles.css";

const API_BASE = import.meta.env.VITE_API_BASE_URL || "http://localhost:8000";

function formatMoney(paise) {
  return new Intl.NumberFormat("en-IN", {
    style: "currency",
    currency: "INR",
    maximumFractionDigits: 2,
    minimumFractionDigits: 2
  }).format((Number(paise) || 0) / 100);
}

function signedMoney(entry) {
  const sign = entry.entry_type === "debit" ? "-" : "+";
  return `${sign}${formatMoney(entry.amount_paise)}`;
}

function cleanRupeeInput(value) {
  const normalized = String(value || "").replace(/,/g, "");
  const cleaned = normalized.replace(/[^\d.]/g, "");
  const [whole, ...rest] = cleaned.split(".");
  return rest.length ? `${whole}.${rest.join("").slice(0, 2)}` : whole;
}

function rupeesToPaise(value) {
  const cleaned = cleanRupeeInput(value);
  if (!cleaned) return 0;
  return Math.round(Number(cleaned) * 100);
}

function shortId(id) {
  if (!id) return "";
  return `${id.slice(0, 7)}...${id.slice(-4)}`;
}

function relativeTime(value) {
  const then = new Date(value).getTime();
  const diff = Math.max(0, Date.now() - then);
  const seconds = Math.floor(diff / 1000);
  if (seconds < 60) return `${seconds || 1}s ago`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  return `${Math.floor(hours / 24)}d ago`;
}

function statusClass(status) {
  return {
    pending: "border-amber-400/25 bg-amber-400/10 text-amber-300",
    processing: "border-sky-400/25 bg-sky-400/10 text-sky-300",
    completed: "border-emerald-400/25 bg-emerald-400/10 text-emerald-300",
    failed: "border-rose-400/25 bg-rose-400/10 text-rose-300"
  }[status] || "border-slate-400/20 bg-slate-400/10 text-slate-300";
}

const EXPLAINER_SECTIONS = [
  {
    number: "01",
    title: "The Ledger",
    body: (
      <>
        Money lives in <Code>ledger_entries</Code>. Every row is either a <Badge tone="green">credit</Badge> or a <Badge tone="red">debit</Badge>. Balance is always computed from database aggregation, never stored as a cached number that can drift.
      </>
    ),
    code: `LedgerEntry.objects.filter(merchant_id=merchant_id).aggregate(
    credits=Coalesce(
        Sum("amount_paise", filter=Q(entry_type="credit")),
        Value(0),
        output_field=BigIntegerField(),
    ),
    debits=Coalesce(
        Sum("amount_paise", filter=Q(entry_type="debit")),
        Value(0),
        output_field=BigIntegerField(),
    ),
)

balance = credits - debits`,
    footnote: (
      <>
        Amounts are stored as <Code>BigIntegerField</Code> paise. The UI converts paise to rupees only for display, so storage and arithmetic never depend on floats.
      </>
    )
  },
  {
    number: "02",
    title: "The Lock - preventing overdraw",
    body: (
      <>
        Two concurrent payout requests must not both spend the same balance. The payout creation path locks the merchant row with <strong>select_for_update()</strong>, recomputes the ledger balance inside that same transaction, then inserts the debit hold.
      </>
    ),
    code: `with transaction.atomic():
    merchant = Merchant.objects.select_for_update().get(id=merchant_id)
    available_balance = Merchant.balance_expression(merchant.id)

    if available_balance < amount_paise:
        return {"detail": "Insufficient available balance."}, 422

    payout = Payout.objects.create(...)
    LedgerEntry.objects.create(
        merchant=merchant,
        payout=payout,
        amount_paise=amount_paise,
        entry_type=LedgerEntry.EntryType.DEBIT,
        kind=LedgerEntry.Kind.PAYOUT_HOLD,
    )`,
    footnote: "The second concurrent transaction waits on the same merchant row. When it resumes, the first debit is already in the ledger aggregation, so the balance check fails cleanly."
  },
  {
    number: "03",
    title: "Idempotency",
    body: (
      <>
        Each payout request requires an <Code>Idempotency-Key</Code>. The API stores one row per merchant and key, along with the request hash and final response.
      </>
    ),
    code: `models.UniqueConstraint(
    fields=["merchant", "key"],
    name="uniq_idempotency_key_per_merchant",
)

idem = IdempotencyKey.objects.select_for_update().get_or_create(...)

if idem.request_hash != request_hash:
    return {"detail": "Idempotency-Key was already used with a different request body."}, 409

if idem.is_complete:
    return idem.response_body, idem.response_status`,
    footnote: "Duplicate submissions with the same key return the original response. Reusing the same key with a different body is rejected."
  },
  {
    number: "04",
    title: "State Machine",
    body: "Payouts do not jump directly to success. They start pending, move to processing, then either complete or fail. Illegal backwards transitions are blocked at the model layer.",
    code: `LEGAL_TRANSITIONS = {
    Status.PENDING: {Status.PROCESSING},
    Status.PROCESSING: {Status.COMPLETED, Status.FAILED},
    Status.COMPLETED: set(),
    Status.FAILED: set(),
}

if new_status not in self.LEGAL_TRANSITIONS[self.status]:
    raise ValidationError(...)`,
    footnote: "Failed payouts create a refund credit in the same atomic transition, so the status change and balance reversal commit together."
  },
  {
    number: "05",
    title: "Worker Simulation",
    body: "Celery processes pending payouts asynchronously. The simulated bank result is intentionally probabilistic: most complete, some fail and refund, and some remain processing to exercise retry behavior.",
    code: `outcome = random.random()

if outcome < 0.70:
    payout.transition_to(Payout.Status.COMPLETED)
elif outcome < 0.90:
    payout.transition_to(Payout.Status.FAILED, failure_reason="Bank rejected the payout.")
else:
    pass  # stays processing until the stuck retry scheduler picks it up`,
    footnote: "Processing payouts stuck longer than 30 seconds are retried with exponential backoff: 30s, 60s, then 120s. After max attempts, they fail and refund atomically."
  },
  {
    number: "06",
    title: "AI Audit",
    body: "The risky AI-shaped version was to fetch ledger rows, sum them in Python, then insert a debit if the local result looked sufficient. That passes a single-request demo but races under concurrent payout requests.",
    code: `# Wrong: Python arithmetic over fetched rows
entries = LedgerEntry.objects.filter(merchant=merchant)
available_balance = sum(
    entry.amount_paise if entry.entry_type == "credit" else -entry.amount_paise
    for entry in entries
)

if available_balance >= amount_paise:
    LedgerEntry.objects.create(amount_paise=amount_paise, entry_type="debit", ...)

# Replacement: DB aggregation under a shared row lock
with transaction.atomic():
    merchant = Merchant.objects.select_for_update().get(id=merchant_id)
    available_balance = Merchant.balance_expression(merchant.id)`,
    footnote: "The fix is not just using a transaction. The payout request must lock a shared database row that every competing payout for that merchant is forced to acquire."
  }
];

function App() {
  const [merchants, setMerchants] = useState([]);
  const [merchantId, setMerchantId] = useState("");
  const [dashboard, setDashboard] = useState(null);
  const [amount, setAmount] = useState("");
  const [bankAccountId, setBankAccountId] = useState("");
  const [message, setMessage] = useState("");
  const [loading, setLoading] = useState(false);
  const [workerBusy, setWorkerBusy] = useState(false);

  const headers = useMemo(() => (merchantId ? { "X-Merchant-Id": merchantId } : {}), [merchantId]);
  const isExplainer = window.location.pathname.toLowerCase() === "/explainer";

  async function loadMerchants() {
    const response = await fetch(`${API_BASE}/api/v1/merchants`);
    const data = await response.json();
    setMerchants(data);
    if (!merchantId && data.length) setMerchantId(data[0].id);
  }

  async function loadDashboard() {
    if (!merchantId) return;
    const response = await fetch(`${API_BASE}/api/v1/dashboard`, { headers });
    const data = await response.json();
    setDashboard(data);
    if (!bankAccountId && data.bank_accounts?.length) setBankAccountId(data.bank_accounts[0].id);
  }

  useEffect(() => {
    loadMerchants();
  }, []);

  useEffect(() => {
    loadDashboard();
    const timer = setInterval(loadDashboard, 3000);
    return () => clearInterval(timer);
  }, [merchantId]);

  async function requestPayout(event) {
    event.preventDefault();
    setLoading(true);
    setMessage("");

    const amountPaise = rupeesToPaise(amount);
    const idempotencyKey = crypto.randomUUID();
    const response = await fetch(`${API_BASE}/api/v1/payouts`, {
      method: "POST",
      headers: {
        ...headers,
        "Content-Type": "application/json",
        "Idempotency-Key": idempotencyKey
      },
      body: JSON.stringify({ amount_paise: amountPaise, bank_account_id: bankAccountId })
    });
    const data = await response.json();
    setLoading(false);

    if (response.ok) {
      setAmount("");
      setMessage(`Payout ${data.status}: ${formatMoney(data.amount_paise)}`);
      await loadDashboard();
    } else {
      setMessage(data.detail || "Payout request failed.");
    }
  }

  async function runWorker(count = 1) {
    setWorkerBusy(true);
    await fetch(`${API_BASE}/api/v1/worker/process-batch`, {
      method: "POST",
      headers: { ...headers, "Content-Type": "application/json" },
      body: JSON.stringify({ count })
    });
    setWorkerBusy(false);
    setTimeout(loadDashboard, 500);
  }

  if (isExplainer) {
    return <ExplainerPage />;
  }

  if (!dashboard) {
    return (
      <main className="flex min-h-screen items-center justify-center bg-night text-slate-100">
        Loading payout engine...
      </main>
    );
  }

  const merchant = dashboard.merchant;
  const payoutHistory = dashboard.payouts;
  const recentActivity = dashboard.recent_ledger.slice(0, 5);

  return (
    <main className="min-h-screen bg-night text-slate-100">
      <header className="sticky top-0 z-20 border-b border-mint/10 bg-night/90 backdrop-blur-xl">
        <div className="mx-auto flex max-w-[1670px] items-center justify-between px-6 py-4 lg:px-8">
          <div className="flex items-center gap-4">
            <div className="flex h-11 w-11 items-center justify-center rounded-[14px] bg-mint font-bold text-[#04110e]">P</div>
            <div>
              <p className="text-xl font-bold leading-6">Playto Pay</p>
              <p className="text-sm font-medium text-slate-400">Payout Engine</p>
            </div>
          </div>
          <a className="hidden items-center gap-2 text-sm font-semibold text-slate-400 hover:text-mint sm:flex" href="/explainer">
            <BookOpen size={18} />
            EXPLAINER.md
          </a>
        </div>
      </header>

      <div className="dashboard-shell mx-auto max-w-[1670px] px-6 py-10 lg:px-8">
        <section className="mb-8 grid gap-8 xl:grid-cols-[1fr_320px]">
          <div>
            <p className="mb-3 text-sm font-semibold uppercase tracking-[0.22em] text-slate-400">Merchant dashboard</p>
            <h1 className="text-[clamp(2.4rem,4.2vw,3.9rem)] font-bold leading-[1.04] tracking-normal">{merchant.name}</h1>
            <p className="mt-2 text-lg font-medium text-slate-400">{merchant.email}</p>
          </div>
          <label className="self-end">
            <span className="sr-only">Merchant</span>
            <div className="relative">
              <select
                className="h-[54px] w-full appearance-none rounded-lg border border-mint/10 bg-panel/70 px-4 pr-11 text-base font-bold text-slate-100 outline-none backdrop-blur-xl transition focus:border-mint/45"
                value={merchantId}
                onChange={(event) => {
                  setMerchantId(event.target.value);
                  setBankAccountId("");
                }}
              >
                {merchants.map((item) => (
                  <option key={item.id} value={item.id}>
                    {item.name}
                  </option>
                ))}
              </select>
              <ChevronDown className="pointer-events-none absolute right-4 top-1/2 -translate-y-1/2 text-slate-400" size={19} />
            </div>
          </label>
        </section>

        <section className="mb-10 grid gap-5 lg:grid-cols-3">
          <MetricCard
            label="Available balance"
            value={formatMoney(merchant.available_balance_paise)}
            hint="Credits - debits + reversals"
            icon={<Wallet size={22} />}
            tone="mint"
          />
          <MetricCard
            label="Held in payouts"
            value={formatMoney(merchant.held_balance_paise)}
            hint="Pending + processing"
            icon={<Clock3 size={22} />}
            tone="amber"
          />
          <MetricCard
            label="Lifetime credits"
            value={formatMoney(merchant.lifetime_credits_paise)}
            hint="All customer payments to date"
            icon={<ArrowUpRight size={22} />}
            tone="sky"
          />
        </section>

        <section className="grid gap-7 xl:grid-cols-[1fr_540px]">
          <div className="space-y-10">
            <form onSubmit={requestPayout} className="panel p-7 md:p-8">
              <div className="mb-8 flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
                <h2 className="text-[26px] font-bold leading-tight">Request a payout</h2>
                <span className="text-sm font-bold text-mint">How concurrency is enforced</span>
              </div>

              <label className="mb-3 block text-sm font-bold uppercase tracking-[0.12em] text-slate-400">Withdrawal amount</label>
              <div className="mb-2 flex h-[68px] items-center gap-3 rounded-lg border border-mint/10 bg-[#080d18]/70 px-5 text-lg backdrop-blur-xl transition focus-within:border-mint/55 focus-within:shadow-[0_0_0_3px_rgba(40,240,177,0.08)]">
                <span className="font-mono text-lg font-bold text-slate-400">₹</span>
                <input
                  className="h-full min-w-0 flex-1 bg-transparent font-mono text-lg font-bold text-slate-100 outline-none placeholder:text-slate-600"
                  inputMode="decimal"
                  placeholder="0.00"
                  value={amount}
                  onChange={(event) => setAmount(cleanRupeeInput(event.target.value))}
                  required
                />
              </div>
              <p className="mb-8 text-sm font-semibold text-slate-400">Available: {formatMoney(merchant.available_balance_paise)}</p>

              <label className="mb-3 block text-sm font-bold uppercase tracking-[0.12em] text-slate-400">Destination</label>
              <div className="relative mb-7">
                <select
                  className="h-[58px] w-full appearance-none rounded-lg border border-mint/10 bg-[#080d18]/70 px-4 pr-12 text-base font-bold text-slate-100 outline-none backdrop-blur-xl focus:border-mint/45"
                  value={bankAccountId}
                  onChange={(event) => setBankAccountId(event.target.value)}
                >
                  {dashboard.bank_accounts.map((account) => (
                    <option key={account.id} value={account.id}>
                      {account.bank_name} .... {account.last4} {account.id.slice(0, 10)}
                    </option>
                  ))}
                </select>
                <ChevronDown className="pointer-events-none absolute right-5 top-1/2 -translate-y-1/2 text-slate-400" size={19} />
              </div>

              <button className="flex h-[58px] w-full items-center justify-center gap-3 rounded-lg bg-mint px-4 text-base font-black text-[#04110e] transition hover:bg-mintSoft disabled:cursor-not-allowed disabled:opacity-55" disabled={loading}>
                <Send size={20} />
                {loading ? "Requesting payout" : "Request payout"}
              </button>
              <p className="mt-7 text-sm font-medium text-slate-400">
                Each request includes an <span className="font-mono font-bold text-mint">Idempotency-Key</span> header. Duplicate submissions with the same key return the original response - no double debits.
              </p>
              {message && <p className="mt-4 rounded-lg border border-mint/10 bg-white/5 px-4 py-3 text-sm font-semibold text-slate-200 backdrop-blur-xl">{message}</p>}
            </form>
          </div>

          <aside className="space-y-6">
            <section className="panel p-6">
              <h2 className="mb-2 text-sm font-black uppercase tracking-[0.12em] text-slate-400">Worker controls</h2>
              <p className="mb-4 text-sm font-medium leading-6 text-slate-400">
                Simulate bank settlement. 70% success, 20% fail, 10% hang. Retry scheduler reclaims hung payouts after 30s, max 3 attempts.
              </p>
              <div className="space-y-3">
                <WorkerButton icon={<Play size={20} />} label="Process pending batch" busy={workerBusy} onClick={() => runWorker(1)} />
                <WorkerButton icon={<RefreshCcw size={20} />} label="Retry stuck payouts" busy={workerBusy} onClick={() => runWorker(1)} />
                <WorkerButton icon={<Zap size={20} />} label="Burst: 5 parallel workers" busy={workerBusy} onClick={() => runWorker(5)} />
              </div>
            </section>

            <section className="panel p-6">
              <h2 className="mb-6 text-sm font-black uppercase tracking-[0.12em] text-slate-400">Recent activity</h2>
              <div className="divide-y divide-mint/10">
                {recentActivity.map((entry) => {
                  const isDebit = entry.entry_type === "debit";
                  return (
                    <div key={entry.id} className="flex items-center gap-4 py-4 first:pt-0 last:pb-0">
                      <div className={`flex h-11 w-11 shrink-0 items-center justify-center rounded-full border ${isDebit ? "border-rose-400/25 bg-rose-400/12 text-rose-300" : "border-mint/25 bg-mint/12 text-mint"}`}>
                        {isDebit ? <ArrowUpRight size={19} /> : <ArrowDownLeft size={19} />}
                      </div>
                      <div className="min-w-0 flex-1">
                        <p className="truncate text-base font-black">{entry.description}</p>
                        <p className="text-sm font-medium text-slate-400">{relativeTime(entry.created_at)}</p>
                      </div>
                      <p className={`font-mono text-sm font-black ${isDebit ? "text-rose-400" : "text-mint"}`}>{signedMoney(entry)}</p>
                    </div>
                  );
                })}
              </div>
            </section>
          </aside>
        </section>

        <section className="panel mt-10 p-6 md:p-7">
          <div className="mb-6 flex items-center justify-between gap-4">
            <h2 className="text-2xl font-bold">Payout history</h2>
            <p className="font-mono text-sm font-bold text-slate-400">{payoutHistory.length} total</p>
          </div>
          <div className="overflow-x-auto">
            <table className="w-full min-w-[820px] text-left">
              <thead>
                <tr className="border-b border-mint/10 text-sm uppercase tracking-[0.12em] text-slate-400">
                  <th className="py-4 pr-6 font-black">Payout ID</th>
                  <th className="py-4 pr-6 font-black">Amount</th>
                  <th className="py-4 pr-6 font-black">Status</th>
                  <th className="py-4 pr-6 font-black">Attempts</th>
                  <th className="py-4 text-right font-black">Created</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-mint/10">
                {payoutHistory.map((payout) => (
                  <tr key={payout.id}>
                    <td className="py-5 pr-6 font-mono text-sm font-bold text-slate-400">{shortId(payout.id)}</td>
                    <td className="py-5 pr-6 font-mono text-base font-black">{formatMoney(payout.amount_paise)}</td>
                    <td className="py-5 pr-6">
                      <span className={`inline-flex rounded-full border px-3 py-1 text-sm font-black uppercase ${statusClass(payout.status)}`}>
                        {payout.status}
                      </span>
                    </td>
                    <td className="py-5 pr-6 font-mono font-black text-slate-400">{payout.attempts}</td>
                    <td className="py-5 text-right font-semibold text-slate-400">{relativeTime(payout.created_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            {!payoutHistory.length && <p className="py-8 text-center font-semibold text-slate-400">No payouts yet.</p>}
          </div>
        </section>

        <footer className="py-20 text-center text-sm font-semibold text-slate-500">
          Built for Playto's Founding Engineer challenge - Ledger-backed - Idempotent - Concurrency-safe
        </footer>
      </div>
    </main>
  );
}

function ExplainerPage() {
  return (
    <main className="min-h-screen bg-night text-slate-100">
      <header className="sticky top-0 z-20 border-b border-mint/10 bg-night/90 backdrop-blur-xl">
        <div className="mx-auto flex max-w-[1670px] items-center justify-between px-6 py-5 lg:px-8">
          <a className="flex items-center gap-2 text-base font-semibold text-slate-400 transition hover:text-mint" href="/">
            <ArrowLeft size={18} />
            Back to dashboard
          </a>
          <p className="text-sm font-bold uppercase tracking-[0.18em] text-slate-400">EXPLAINER.MD</p>
        </div>
      </header>

      <div className="dashboard-shell min-h-screen">
        <article className="mx-auto max-w-[900px] px-6 py-16 md:py-20">
          <p className="mb-5 text-sm font-bold uppercase tracking-[0.32em] text-mint">Playto payout engine</p>
          <h1 className="text-[clamp(2.7rem,5vw,4rem)] font-bold leading-[1.06] tracking-normal">
            How this was built - and why.
          </h1>
          <p className="mt-6 max-w-[820px] text-xl font-medium leading-9 text-slate-300">
            The challenge asks for ledger-backed payouts with paise storage, database aggregation, idempotency, atomic refunds, and asynchronous payout simulation. The answers below show where each requirement is enforced in the code.
          </p>

          <div className="mt-14 space-y-16">
            {EXPLAINER_SECTIONS.map((section) => (
              <section key={section.number}>
                <div className="mb-6 flex items-center gap-4">
                  <span className="rounded-full border border-mint/10 bg-slate-900/60 px-3 py-1 font-mono text-sm text-sky-300">
                    {section.number}
                  </span>
                  <h2 className="text-[clamp(2rem,3.6vw,2.55rem)] font-bold leading-tight">{section.title}</h2>
                </div>
                <p className="text-xl font-medium leading-9 text-slate-300">{section.body}</p>
                <pre className="mt-5 overflow-x-auto rounded-[14px] border border-mint/10 bg-[#070d18]/75 p-5 text-[15px] font-bold leading-6 text-mint shadow-[0_18px_55px_rgba(0,0,0,0.24)] backdrop-blur-xl">
                  <code>{section.code}</code>
                </pre>
                <p className="mt-5 text-xl font-medium leading-9 text-slate-300">{section.footnote}</p>
              </section>
            ))}
          </div>
        </article>
      </div>
    </main>
  );
}

function Code({ children }) {
  return <code className="rounded-md bg-mint/10 px-1.5 py-0.5 font-mono text-mint">{children}</code>;
}

function Badge({ children, tone }) {
  const classes = {
    green: "border-mint/25 bg-mint/10 text-mint",
    red: "border-rose-400/25 bg-rose-400/10 text-rose-300"
  }[tone];

  return <span className={`rounded-full border px-2 py-0.5 font-mono text-sm font-bold ${classes}`}>{children}</span>;
}

function MetricCard({ icon, label, value, hint, tone }) {
  const toneClass = {
    mint: "bg-mint/15 text-mint",
    amber: "bg-amber-400/15 text-amber-300",
    sky: "bg-sky-400/15 text-sky-300"
  }[tone];

  return (
    <div className="metric-card flex min-h-[196px] flex-col rounded-[18px] border border-mint/10 bg-panel/65 p-7">
      <div className="mb-5 flex justify-end">
        <div className={`flex h-10 w-10 items-center justify-center rounded-full ${toneClass}`}>{icon}</div>
      </div>
      <p className="mb-5 text-sm font-bold uppercase tracking-[0.12em] text-slate-400">{label}</p>
      <p className="font-mono text-[clamp(1.9rem,2.35vw,2.6rem)] font-black leading-none tracking-normal">{value}</p>
      <p className="mt-4 text-base font-medium text-slate-400">{hint}</p>
    </div>
  );
}

function WorkerButton({ icon, label, busy, onClick }) {
  return (
    <button
      className="flex h-[45px] w-full items-center justify-center gap-3 rounded-lg bg-slate-800/70 px-4 text-base font-black text-slate-100 backdrop-blur-xl transition hover:bg-slate-700/80 disabled:opacity-60"
      disabled={busy}
      onClick={onClick}
      type="button"
    >
      {icon}
      {label}
    </button>
  );
}

createRoot(document.getElementById("root")).render(<App />);
