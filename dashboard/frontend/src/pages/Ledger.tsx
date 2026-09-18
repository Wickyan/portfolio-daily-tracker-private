import { useEffect, useMemo, useRef, useState } from 'react'
import {
  AlertTriangle,
  CalendarDays,
  CheckCircle2,
  Clock3,
  History,
  Mic,
  MicOff,
  RefreshCw,
  Search,
  Send,
  WalletCards,
} from 'lucide-react'

import { getApiErrorMessage } from '@/services/api'
import ledgerService, {
  type LedgerEvent,
  type LedgerPreview,
  type LedgerState,
} from '@/services/ledger'

type SpeechRecognitionLike = {
  lang: string
  continuous: boolean
  interimResults: boolean
  onresult: ((event: any) => void) | null
  onerror: ((event: any) => void) | null
  onend: (() => void) | null
  start: () => void
  stop: () => void
}

type SpeechRecognitionCtor = new () => SpeechRecognitionLike

const ACTION_LABELS: Record<string, string> = {
  BUY: '买入',
  SELL: '卖出',
  OPENING_POSITION: '初始持仓',
  OPENING_CASH: '初始现金',
  DEPOSIT: '入金',
  WITHDRAW: '出金',
  FX: '换汇',
  DIVIDEND: '分红',
  TRANSFER_IN: '转入',
  TRANSFER_OUT: '转出',
  SPLIT: '拆股',
  REVERSAL: '冲销',
}

function actionLabel(type: string) {
  return ACTION_LABELS[type] || type
}

function actionClass(type: string) {
  if (type === 'BUY' || type === 'DEPOSIT' || type === 'DIVIDEND') {
    return 'border-emerald-500/40 bg-emerald-500/10 text-emerald-300'
  }
  if (type === 'SELL' || type === 'WITHDRAW') {
    return 'border-rose-500/40 bg-rose-500/10 text-rose-300'
  }
  if (type.startsWith('OPENING')) {
    return 'border-sky-500/40 bg-sky-500/10 text-sky-300'
  }
  return 'border-slate-500/40 bg-slate-500/10 text-slate-300'
}

function displayTime(value: string) {
  return value ? value.replace('T', ' ') : '—'
}

function compactNumber(value?: string | null) {
  if (value === null || value === undefined || value === '') return '—'
  const number = Number(value)
  if (!Number.isFinite(number)) return value
  return number.toLocaleString(undefined, { maximumFractionDigits: 6 })
}

function eventDescription(event: LedgerEvent) {
  if (['BUY', 'SELL', 'OPENING_POSITION', 'TRANSFER_IN', 'TRANSFER_OUT'].includes(event.event_type)) {
    return [
      event.name || event.code,
      event.quantity ? `${compactNumber(event.quantity)} 股/份` : null,
      event.price && event.currency ? `@${compactNumber(event.price)} ${event.currency}` : null,
      event.fee && Number(event.fee) !== 0 ? `手续费 ${compactNumber(event.fee)}` : null,
    ]
      .filter(Boolean)
      .join(' · ')
  }

  if (event.event_type === 'FX') {
    return `${compactNumber(event.amount)} ${event.currency} → ${compactNumber(event.counter_amount)} ${event.counter_currency}`
  }

  return event.amount && event.currency
    ? `${compactNumber(event.amount)} ${event.currency}`
    : event.code || event.name || '账户事件'
}

function getSpeechRecognitionCtor(): SpeechRecognitionCtor | null {
  const browserWindow = window as typeof window & {
    SpeechRecognition?: SpeechRecognitionCtor
    webkitSpeechRecognition?: SpeechRecognitionCtor
  }
  return browserWindow.SpeechRecognition || browserWindow.webkitSpeechRecognition || null
}

export default function Ledger() {
  const [input, setInput] = useState('')
  const [inputSource, setInputSource] = useState<'text' | 'voice'>('text')
  const [pending, setPending] = useState<LedgerPreview | null>(null)
  const [transactions, setTransactions] = useState<LedgerEvent[]>([])
  const [state, setState] = useState<LedgerState | null>(null)
  const [asOf, setAsOf] = useState('')
  const [filter, setFilter] = useState('')
  const [loading, setLoading] = useState(false)
  const [confirming, setConfirming] = useState(false)
  const [refreshing, setRefreshing] = useState(false)
  const [listening, setListening] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const recognitionRef = useRef<SpeechRecognitionLike | null>(null)

  const refreshAll = async (stateDate?: string) => {
    setRefreshing(true)
    setError('')
    try {
      const [history, nextState] = await Promise.all([
        ledgerService.getTransactions(),
        ledgerService.getState(stateDate),
      ])
      setTransactions(history.transactions)
      setState(nextState)
    } catch (err) {
      setError(getApiErrorMessage(err))
    } finally {
      setRefreshing(false)
    }
  }

  useEffect(() => {
    void refreshAll()
    return () => recognitionRef.current?.stop()
  }, [])

  const filteredTransactions = useMemo(() => {
    const needle = filter.trim().toLowerCase()
    const rows = [...transactions].reverse()
    if (!needle) return rows
    return rows.filter((event) =>
      [
        event.account,
        event.code,
        event.name,
        event.event_type,
        event.currency,
        event.effective_at,
      ]
        .filter(Boolean)
        .some((value) => String(value).toLowerCase().includes(needle)),
    )
  }, [transactions, filter])

  const startVoice = () => {
    setError('')
    setNotice('')
    const Recognition = getSpeechRecognitionCtor()
    if (!Recognition) {
      setError('当前浏览器不支持语音识别。可以先使用文字输入，或换用支持 Web Speech API 的浏览器。')
      return
    }

    const recognition = new Recognition()
    recognition.lang = 'zh-CN'
    recognition.continuous = false
    recognition.interimResults = false
    const base = input.trim()

    recognition.onresult = (event: any) => {
      const transcript = String(event.results?.[0]?.[0]?.transcript || '').trim()
      if (!transcript) return
      setInput(base ? `${base}；${transcript}` : transcript)
      setInputSource('voice')
      setNotice('语音已转成文字，请先核对文字，再生成确认卡。')
    }
    recognition.onerror = (event: any) => {
      setError(`语音识别失败：${event.error || '未知错误'}`)
    }
    recognition.onend = () => {
      setListening(false)
      recognitionRef.current = null
    }

    recognitionRef.current = recognition
    setListening(true)
    recognition.start()
  }

  const stopVoice = () => {
    recognitionRef.current?.stop()
    recognitionRef.current = null
    setListening(false)
  }

  const previewInput = async () => {
    if (!input.trim()) {
      setError('请先输入或说出一条交易记录。')
      return
    }
    setLoading(true)
    setError('')
    setNotice('')
    try {
      const result = await ledgerService.previewText(
        input.trim(),
        inputSource,
        ledgerService.localReferenceTime(),
      )
      setPending(result)
      if (result.historical_backfill) {
        setNotice('这是历史补录。确认后系统会从历史时间点重新计算后续持仓和成本。')
      } else {
        setNotice('已生成待确认记录。当前还没有写入交易账本。')
      }
    } catch (err) {
      setPending(null)
      setError(getApiErrorMessage(err))
    } finally {
      setLoading(false)
    }
  }

  const confirmPending = async () => {
    if (!pending) return
    setConfirming(true)
    setError('')
    try {
      const result = await ledgerService.confirm(pending.pending_id)
      setNotice(
        result.historical_backfill
          ? '历史交易已写入，并已按完整流水重新计算当前状态。'
          : '交易已写入 V3 交易账本。',
      )
      setPending(null)
      setInput('')
      setInputSource('text')
      await refreshAll(asOf || undefined)
    } catch (err) {
      setError(getApiErrorMessage(err))
    } finally {
      setConfirming(false)
    }
  }

  const queryAsOf = async () => {
    await refreshAll(asOf || undefined)
    setNotice(asOf ? `正在显示 ${asOf} 日终的账本状态。` : '已恢复显示当前账本状态。')
  }

  return (
    <div className="space-y-6 p-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="flex items-center gap-2 text-2xl font-bold text-white">
            <History className="h-6 w-6 text-primary-400" />
            交易账本
          </h1>
          <p className="mt-1 text-sm text-slate-400">
            每笔历史交易是事实源；当前持仓由完整流水重放得到。
          </p>
        </div>
        <button
          onClick={() => void refreshAll(asOf || undefined)}
          disabled={refreshing}
          className="flex items-center gap-2 rounded-lg border border-slate-600 px-3 py-2 text-sm text-slate-200 hover:bg-slate-700 disabled:opacity-50"
        >
          <RefreshCw className={`h-4 w-4 ${refreshing ? 'animate-spin' : ''}`} />
          刷新
        </button>
      </div>

      {error && (
        <div className="flex items-start gap-2 rounded-lg border border-rose-500/40 bg-rose-500/10 p-3 text-sm text-rose-200">
          <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
          <span>{error}</span>
        </div>
      )}
      {notice && (
        <div className="flex items-start gap-2 rounded-lg border border-sky-500/30 bg-sky-500/10 p-3 text-sm text-sky-200">
          <CheckCircle2 className="mt-0.5 h-4 w-4 shrink-0" />
          <span>{notice}</span>
        </div>
      )}

      <section className="rounded-xl border border-slate-700 bg-slate-800 p-5">
        <div className="mb-3 flex items-center justify-between gap-3">
          <div>
            <h2 className="font-semibold text-white">新增 / 补录交易</h2>
            <p className="mt-1 text-xs text-slate-400">
              例：去年3月12号在长桥买了2股英伟达，87.5美元一股，手续费1美元
            </p>
          </div>
          <span className="rounded-full bg-slate-700 px-2.5 py-1 text-xs text-slate-300">
            {inputSource === 'voice' ? '语音来源' : '文字来源'}
          </span>
        </div>

        <textarea
          value={input}
          onChange={(event) => {
            setInput(event.target.value)
            setInputSource('text')
          }}
          rows={4}
          placeholder="可以输入今天的交易，也可以一次性补录任意历史日期……"
          className="w-full resize-y rounded-lg border border-slate-600 bg-slate-900 px-3 py-3 text-sm text-slate-100 outline-none transition focus:border-primary-500"
        />

        <div className="mt-3 flex flex-wrap gap-2">
          <button
            type="button"
            onClick={listening ? stopVoice : startVoice}
            className={`flex items-center gap-2 rounded-lg px-3 py-2 text-sm font-medium transition ${
              listening
                ? 'bg-rose-600 text-white hover:bg-rose-500'
                : 'border border-slate-600 bg-slate-700 text-slate-100 hover:bg-slate-600'
            }`}
          >
            {listening ? <MicOff className="h-4 w-4" /> : <Mic className="h-4 w-4" />}
            {listening ? '停止聆听' : '语音输入'}
          </button>
          <button
            type="button"
            onClick={() => void previewInput()}
            disabled={loading || !input.trim()}
            className="flex items-center gap-2 rounded-lg bg-primary-600 px-4 py-2 text-sm font-medium text-white hover:bg-primary-500 disabled:cursor-not-allowed disabled:opacity-50"
          >
            <Send className="h-4 w-4" />
            {loading ? '解析中…' : '生成确认卡'}
          </button>
          <button
            type="button"
            onClick={() => {
              setInput('')
              setPending(null)
              setError('')
              setNotice('')
              setInputSource('text')
            }}
            className="rounded-lg px-3 py-2 text-sm text-slate-400 hover:bg-slate-700 hover:text-slate-200"
          >
            清空
          </button>
        </div>
      </section>

      {pending && (
        <section className="rounded-xl border border-amber-500/40 bg-amber-500/5 p-5">
          <div className="mb-4 flex flex-wrap items-start justify-between gap-3">
            <div>
              <h2 className="flex items-center gap-2 font-semibold text-amber-200">
                <Clock3 className="h-5 w-5" />
                待确认交易
              </h2>
              <p className="mt-1 text-xs text-slate-400">
                pending {pending.pending_id.slice(0, 8)} · 到期 {displayTime(pending.expires_at)}
              </p>
            </div>
            {pending.historical_backfill && (
              <span className="rounded-full border border-amber-500/40 bg-amber-500/10 px-2.5 py-1 text-xs text-amber-200">
                历史回填 · 将重算后续状态
              </span>
            )}
          </div>

          <div className="space-y-3">
            {pending.events.map((event, index) => (
              <div
                key={event.transaction_id}
                className="rounded-lg border border-slate-700 bg-slate-900/70 p-4"
              >
                <div className="flex flex-wrap items-center gap-2">
                  <span className="text-xs text-slate-500">第 {index + 1} 条</span>
                  <span
                    className={`rounded border px-2 py-0.5 text-xs ${actionClass(event.event_type)}`}
                  >
                    {actionLabel(event.event_type)}
                  </span>
                  <span className="font-medium text-slate-100">{event.account}</span>
                  <span className="text-sm text-slate-300">{eventDescription(event)}</span>
                </div>
                <div className="mt-2 grid gap-2 text-xs text-slate-400 sm:grid-cols-2 lg:grid-cols-4">
                  <div>实际发生：{displayTime(event.effective_at)}</div>
                  <div>录入时间：{displayTime(event.entered_at)}</div>
                  <div>代码：{event.code || '—'}</div>
                  <div>来源：{event.source}</div>
                </div>
              </div>
            ))}
          </div>

          {pending.interpretation?.warnings?.length ? (
            <div className="mt-4 rounded-lg border border-amber-500/20 bg-amber-500/5 p-3">
              <div className="mb-1 text-xs font-medium text-amber-200">解析提醒</div>
              {pending.interpretation.warnings.map((warning) => (
                <div key={warning} className="text-xs text-amber-100/80">
                  • {warning}
                </div>
              ))}
            </div>
          ) : null}

          <div className="mt-4 flex flex-wrap gap-2">
            <button
              onClick={() => void confirmPending()}
              disabled={confirming}
              className="flex items-center gap-2 rounded-lg bg-emerald-600 px-4 py-2 text-sm font-medium text-white hover:bg-emerald-500 disabled:opacity-50"
            >
              <CheckCircle2 className="h-4 w-4" />
              {confirming ? '写入中…' : `确认写入 ${pending.events.length} 条`}
            </button>
            <button
              onClick={() => setPending(null)}
              disabled={confirming}
              className="rounded-lg border border-slate-600 px-3 py-2 text-sm text-slate-300 hover:bg-slate-700 disabled:opacity-50"
            >
              暂不写入
            </button>
          </div>
        </section>
      )}

      <section className="rounded-xl border border-slate-700 bg-slate-800 p-5">
        <div className="flex flex-wrap items-end justify-between gap-3">
          <div>
            <h2 className="flex items-center gap-2 font-semibold text-white">
              <WalletCards className="h-5 w-5 text-primary-400" />
              {asOf ? `${asOf} 日终状态` : '当前账本状态'}
            </h2>
            <p className="mt-1 text-xs text-slate-400">
              已重放 {state?.applied_transactions ?? 0} 条交易事件
            </p>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <div className="relative">
              <CalendarDays className="pointer-events-none absolute left-2.5 top-2.5 h-4 w-4 text-slate-500" />
              <input
                type="date"
                value={asOf}
                onChange={(event) => setAsOf(event.target.value)}
                className="rounded-lg border border-slate-600 bg-slate-900 py-2 pl-8 pr-3 text-sm text-slate-200"
              />
            </div>
            <button
              onClick={() => void queryAsOf()}
              className="rounded-lg border border-slate-600 px-3 py-2 text-sm text-slate-200 hover:bg-slate-700"
            >
              查询
            </button>
            {asOf && (
              <button
                onClick={() => {
                  setAsOf('')
                  void refreshAll()
                  setNotice('已恢复显示当前账本状态。')
                }}
                className="px-2 py-2 text-sm text-slate-400 hover:text-white"
              >
                回到今天
              </button>
            )}
          </div>
        </div>

        <div className="mt-4 overflow-x-auto rounded-lg border border-slate-700">
          <table className="min-w-full text-sm">
            <thead className="bg-slate-900/70 text-left text-xs text-slate-400">
              <tr>
                <th className="px-3 py-2">账户</th>
                <th className="px-3 py-2">标的</th>
                <th className="px-3 py-2 text-right">数量</th>
                <th className="px-3 py-2 text-right">平均成本</th>
                <th className="px-3 py-2 text-right">总成本</th>
                <th className="px-3 py-2 text-right">已实现盈亏</th>
                <th className="px-3 py-2 text-right">分红</th>
              </tr>
            </thead>
            <tbody>
              {(state?.positions || []).map((position) => (
                <tr
                  key={`${position.account}-${position.code}-${position.currency}`}
                  className="border-t border-slate-700 text-slate-200"
                >
                  <td className="px-3 py-2">{position.account}</td>
                  <td className="px-3 py-2">
                    <div className="font-medium">{position.code}</div>
                    <div className="text-xs text-slate-500">{position.name}</div>
                  </td>
                  <td className="px-3 py-2 text-right">{compactNumber(position.quantity)}</td>
                  <td className="px-3 py-2 text-right">
                    {compactNumber(position.average_cost)} {position.currency}
                  </td>
                  <td className="px-3 py-2 text-right">{compactNumber(position.total_cost)}</td>
                  <td
                    className={`px-3 py-2 text-right ${
                      Number(position.realized_pnl) >= 0 ? 'text-emerald-300' : 'text-rose-300'
                    }`}
                  >
                    {compactNumber(position.realized_pnl)}
                  </td>
                  <td className="px-3 py-2 text-right">{compactNumber(position.dividend_income)}</td>
                </tr>
              ))}
              {!state?.positions?.length && (
                <tr>
                  <td colSpan={7} className="px-3 py-8 text-center text-slate-500">
                    当前日期没有证券持仓。
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>

        {state?.cash_accounts?.length ? (
          <div className="mt-3 flex flex-wrap gap-2">
            {state.cash_accounts.map((cash) => (
              <div
                key={`${cash.account}-${cash.currency}`}
                className="rounded-lg border border-slate-700 bg-slate-900 px-3 py-2 text-sm"
              >
                <span className="text-slate-400">{cash.account}</span>
                <span className="ml-2 font-medium text-slate-100">
                  {compactNumber(cash.amount)} {cash.currency}
                </span>
              </div>
            ))}
          </div>
        ) : null}
      </section>

      <section className="rounded-xl border border-slate-700 bg-slate-800 p-5">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div>
            <h2 className="font-semibold text-white">交易时间线</h2>
            <p className="mt-1 text-xs text-slate-400">共 {transactions.length} 条已确认事件</p>
          </div>
          <div className="relative w-full sm:w-72">
            <Search className="pointer-events-none absolute left-2.5 top-2.5 h-4 w-4 text-slate-500" />
            <input
              value={filter}
              onChange={(event) => setFilter(event.target.value)}
              placeholder="筛选代码、账户、名称、日期…"
              className="w-full rounded-lg border border-slate-600 bg-slate-900 py-2 pl-8 pr-3 text-sm text-slate-200 outline-none focus:border-primary-500"
            />
          </div>
        </div>

        <div className="mt-4 space-y-2">
          {filteredTransactions.map((event) => (
            <div
              key={event.transaction_id}
              className="grid gap-2 rounded-lg border border-slate-700 bg-slate-900/60 p-3 md:grid-cols-[190px_110px_130px_1fr]"
            >
              <div className="text-xs text-slate-400">{displayTime(event.effective_at)}</div>
              <div>
                <span
                  className={`rounded border px-2 py-0.5 text-xs ${actionClass(event.event_type)}`}
                >
                  {actionLabel(event.event_type)}
                </span>
              </div>
              <div className="text-sm font-medium text-slate-200">{event.account}</div>
              <div className="text-sm text-slate-300">{eventDescription(event)}</div>
            </div>
          ))}
          {!filteredTransactions.length && (
            <div className="py-8 text-center text-sm text-slate-500">暂无匹配的交易流水。</div>
          )}
        </div>
      </section>
    </div>
  )
}
