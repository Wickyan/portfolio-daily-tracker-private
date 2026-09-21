import { useEffect, useMemo, useRef, useState } from 'react'
import {
  AlertTriangle,
  CalendarDays,
  CheckCircle2,
  Clock3,
  History,
  ImagePlus,
  Mic,
  MicOff,
  RefreshCw,
  Search,
  Send,
  Trash2,
  Undo2,
  WalletCards,
} from 'lucide-react'

import { getApiErrorMessage } from '@/services/api'
import ledgerService, {
  type LedgerEvent,
  type LedgerPreview,
  type LedgerScreenshotPreview,
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

  if (event.event_type === 'REVERSAL') {
    const target = event.reverses_transaction_id?.slice(0, 8) || '未知记录'
    return `冲销交易 ${target}${event.note ? ` · ${event.note}` : ''}`
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
  const [reversingId, setReversingId] = useState('')
  const [refreshing, setRefreshing] = useState(false)
  const [listening, setListening] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [screenshotFiles, setScreenshotFiles] = useState<File[]>([])
  const [screenshotAccount, setScreenshotAccount] = useState('')
  const [screenshotLoading, setScreenshotLoading] = useState(false)
  const [screenshotResult, setScreenshotResult] = useState<LedgerScreenshotPreview | null>(null)
  const recognitionRef = useRef<SpeechRecognitionLike | null>(null)
  const screenshotInputRef = useRef<HTMLInputElement | null>(null)

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

  const addScreenshotFiles = (incoming: File[]) => {
    const valid = incoming.filter((file) => {
      const looksLikeImage =
        file.type.startsWith('image/') || /\.(png|jpe?g|webp)$/i.test(file.name)
      return looksLikeImage && file.size <= 50 * 1024 * 1024
    })
    const rejected = incoming.length - valid.length
    setScreenshotFiles((current) => {
      const seen = new Set(current.map((file) => `${file.name}:${file.size}:${file.lastModified}`))
      const next = [...current]
      for (const file of valid) {
        const key = `${file.name}:${file.size}:${file.lastModified}`
        if (!seen.has(key) && next.length < 12) {
          seen.add(key)
          next.push(file)
        }
      }
      return next
    })
    setScreenshotResult(null)
    if (rejected > 0) {
      setError(`有 ${rejected} 个文件不是支持的图片或超过单张50MB限制。`)
    } else if (incoming.length > 12) {
      setError('一次最多选择12张截图。')
    } else {
      setError('')
    }
  }

  const previewScreenshotInput = async () => {
    if (!screenshotFiles.length) {
      setError('请先选择至少一张订单截图。')
      return
    }
    setScreenshotLoading(true)
    setError('')
    setNotice('')
    try {
      const result = await ledgerService.previewScreenshots(
        screenshotFiles,
        screenshotAccount,
      )
      setScreenshotResult(result)
      if (result.pending_id && result.created_at && result.expires_at) {
        setPending({
          pending_id: result.pending_id,
          status: result.status,
          created_at: result.created_at,
          expires_at: result.expires_at,
          historical_backfill: result.historical_backfill,
          events: result.events,
          projected_state: result.projected_state,
        })
        setNotice(
          `截图识别出 ${result.events.length} 条新订单；已去重 ${result.extraction.duplicate_count} 条，无法确定 ${result.extraction.unresolved_count} 条。请核对确认卡后再写入。`,
        )
      } else if (result.extraction.duplicate_count > 0 && result.extraction.unresolved_count === 0) {
        setNotice('截图中的订单都已存在于账本/待确认记录中，没有生成新的确认卡。')
      } else {
        setNotice('没有生成可写入的新订单；请查看下方无法确定字段或去重结果。')
      }
    } catch (err) {
      setScreenshotResult(null)
      setError(getApiErrorMessage(err))
    } finally {
      setScreenshotLoading(false)
    }
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
    const confirmingScreenshot = pending.events.some((event) => event.source === 'screenshot')
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
      if (confirmingScreenshot) {
        setScreenshotFiles([])
        setScreenshotResult(null)
      }
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


  const previewReversal = async (event: LedgerEvent) => {
    if (event.event_type === 'REVERSAL' || event.is_reversed) return
    const reason = window.prompt(
      '冲销不会删除原记录，而是新增一条 REVERSAL 审计事件。可填写原因（可留空）：',
      '',
    )
    if (reason === null) return

    setReversingId(event.transaction_id)
    setError('')
    setNotice('')
    try {
      const result = await ledgerService.previewReverse(event.transaction_id, reason.trim())
      setPending(result)
      setNotice('已生成冲销确认卡。原交易尚未改变；请核对后再确认写入。')
      window.scrollTo({ top: 0, behavior: 'smooth' })
    } catch (err) {
      setError(getApiErrorMessage(err))
    } finally {
      setReversingId('')
    }
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

      <section
        className="rounded-xl border border-slate-700 bg-slate-800 p-5"
        tabIndex={0}
        onDragOver={(event) => event.preventDefault()}
        onDrop={(event) => {
          event.preventDefault()
          addScreenshotFiles(Array.from(event.dataTransfer.files))
        }}
        onPaste={(event) => {
          const files = Array.from(event.clipboardData.files)
          if (files.length) addScreenshotFiles(files)
        }}
      >
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div>
            <h2 className="flex items-center gap-2 font-semibold text-white">
              <ImagePlus className="h-5 w-5 text-primary-400" />
              历史订单截图导入
            </h2>
            <p className="mt-1 text-xs text-slate-400">
              支持短截图、超长截图和多张截图。长图会自动重叠切片；相同订单会在切片、多图、待确认和已确认账本之间去重。
            </p>
          </div>
          <span className="rounded-full border border-slate-600 bg-slate-900 px-2.5 py-1 text-xs text-slate-400">
            最多12张 · 单张50MB
          </span>
        </div>

        <div className="mt-4 grid gap-3 lg:grid-cols-[1fr_260px]">
          <div
            className="rounded-lg border border-dashed border-slate-600 bg-slate-900/60 p-5 text-center"
          >
            <input
              ref={screenshotInputRef}
              type="file"
              multiple
              accept="image/png,image/jpeg,image/webp,image/*"
              className="hidden"
              onChange={(event) => {
                addScreenshotFiles(Array.from(event.target.files || []))
                event.target.value = ''
              }}
            />
            <ImagePlus className="mx-auto h-8 w-8 text-slate-500" />
            <div className="mt-2 text-sm text-slate-300">拖进来、粘贴截图，或选择图片</div>
            <div className="mt-1 text-xs text-slate-500">PNG / JPEG / WebP；超长图无需手工裁剪</div>
            <button
              type="button"
              onClick={() => screenshotInputRef.current?.click()}
              className="mt-3 rounded-lg border border-slate-600 bg-slate-700 px-3 py-2 text-sm text-slate-100 hover:bg-slate-600"
            >
              选择截图
            </button>
          </div>

          <div className="rounded-lg border border-slate-700 bg-slate-900/60 p-4">
            <label className="text-xs font-medium text-slate-300">账户提示（可选）</label>
            <input
              value={screenshotAccount}
              onChange={(event) => setScreenshotAccount(event.target.value)}
              placeholder="如：长桥 / IBKR"
              className="mt-2 w-full rounded-lg border border-slate-600 bg-slate-900 px-3 py-2 text-sm text-slate-100 outline-none focus:border-primary-500"
            />
            <p className="mt-2 text-[11px] leading-5 text-slate-500">
              截图本身看不出账户时才使用；截图能明确读到账户时，以截图内容为准。
            </p>
          </div>
        </div>

        {screenshotFiles.length > 0 && (
          <div className="mt-4 space-y-2">
            {screenshotFiles.map((file, index) => (
              <div
                key={`${file.name}-${file.size}-${file.lastModified}`}
                className="flex items-center justify-between gap-3 rounded-lg border border-slate-700 bg-slate-900/60 px-3 py-2"
              >
                <div className="min-w-0">
                  <div className="truncate text-sm text-slate-200">
                    {index + 1}. {file.name}
                  </div>
                  <div className="text-xs text-slate-500">
                    {(file.size / 1024 / 1024).toFixed(2)} MB
                  </div>
                </div>
                <button
                  type="button"
                  onClick={() => {
                    setScreenshotFiles((files) => files.filter((_, itemIndex) => itemIndex !== index))
                    setScreenshotResult(null)
                  }}
                  className="rounded p-1.5 text-slate-500 hover:bg-slate-700 hover:text-rose-300"
                  aria-label="移除截图"
                >
                  <Trash2 className="h-4 w-4" />
                </button>
              </div>
            ))}
          </div>
        )}

        <div className="mt-4 flex flex-wrap gap-2">
          <button
            type="button"
            onClick={() => void previewScreenshotInput()}
            disabled={screenshotLoading || screenshotFiles.length === 0}
            className="flex items-center gap-2 rounded-lg bg-primary-600 px-4 py-2 text-sm font-medium text-white hover:bg-primary-500 disabled:cursor-not-allowed disabled:opacity-50"
          >
            <ImagePlus className="h-4 w-4" />
            {screenshotLoading ? '正在切图并识别…' : `识别 ${screenshotFiles.length || ''} 张截图`}
          </button>
          {screenshotFiles.length > 0 && (
            <button
              type="button"
              onClick={() => {
                setScreenshotFiles([])
                setScreenshotResult(null)
              }}
              disabled={screenshotLoading}
              className="rounded-lg px-3 py-2 text-sm text-slate-400 hover:bg-slate-700 hover:text-slate-200 disabled:opacity-50"
            >
              清空截图
            </button>
          )}
        </div>

        {screenshotResult && (
          <div className="mt-5 space-y-3">
            <div className="grid gap-2 sm:grid-cols-3">
              <div className="rounded-lg border border-emerald-500/30 bg-emerald-500/10 p-3">
                <div className="text-xs text-emerald-300">新订单</div>
                <div className="mt-1 text-xl font-semibold text-white">{screenshotResult.events.length}</div>
              </div>
              <div className="rounded-lg border border-sky-500/30 bg-sky-500/10 p-3">
                <div className="text-xs text-sky-300">自动去重 / 非成交</div>
                <div className="mt-1 text-xl font-semibold text-white">
                  {screenshotResult.extraction.duplicate_count}
                </div>
              </div>
              <div className="rounded-lg border border-amber-500/30 bg-amber-500/10 p-3">
                <div className="text-xs text-amber-300">字段不完整</div>
                <div className="mt-1 text-xl font-semibold text-white">
                  {screenshotResult.extraction.unresolved_count}
                </div>
              </div>
            </div>

            <div className="rounded-lg border border-slate-700 bg-slate-900/60 p-3 text-xs text-slate-400">
              <div>识别方式：{screenshotResult.extraction.model}</div>
              {screenshotResult.extraction.images.map((image) => (
                <div key={image.sha256} className="mt-1">
                  {image.filename} · {image.width}×{image.height} · {image.tile_count} 个切片 ·
                  识别原始行 {image.raw_rows}
                </div>
              ))}
            </div>

            {screenshotResult.extraction.warnings.length > 0 && (
              <div className="rounded-lg border border-amber-500/30 bg-amber-500/5 p-3">
                <div className="text-xs font-medium text-amber-200">截图识别提醒</div>
                <div className="mt-2 space-y-1">
                  {Array.from(new Set(screenshotResult.extraction.warnings)).slice(0, 30).map((warning) => (
                    <div key={warning} className="text-xs text-amber-100/80">
                      • {warning}
                    </div>
                  ))}
                </div>
              </div>
            )}

            {screenshotResult.extraction.duplicates.length > 0 && (
              <div className="rounded-lg border border-sky-500/20 bg-sky-500/5 p-3">
                <div className="text-xs font-medium text-sky-200">已自动去重 / 排除</div>
                <div className="mt-2 space-y-1">
                  {screenshotResult.extraction.duplicates.slice(0, 20).map((item, index) => (
                    <div key={index} className="text-xs text-slate-400">
                      • {String(item.raw.raw_text || item.raw.code || item.raw.filename || '重复记录')}
                      <span className="ml-2 text-sky-400/80">[{item.duplicate_reason || 'duplicate'}]</span>
                    </div>
                  ))}
                  {screenshotResult.extraction.duplicates.length > 20 && (
                    <div className="text-xs text-slate-500">
                      另有 {screenshotResult.extraction.duplicates.length - 20} 条未展开
                    </div>
                  )}
                </div>
              </div>
            )}

            {screenshotResult.extraction.unresolved.length > 0 && (
              <div className="rounded-lg border border-amber-500/30 bg-amber-500/5 p-3">
                <div className="text-xs font-medium text-amber-200">
                  以下记录不会自动写入，请补充信息后重新识别
                </div>
                <div className="mt-2 space-y-2">
                  {screenshotResult.extraction.unresolved.slice(0, 20).map((item, index) => (
                    <div key={index} className="rounded border border-slate-700 bg-slate-900/50 p-2 text-xs">
                      <div className="text-slate-300">
                        {String(item.raw.raw_text || item.raw.code || item.raw.name || '无法完整识别的记录')}
                      </div>
                      <div className="mt-1 text-amber-300">
                        缺少：{item.missing_fields.join('、') || '未知字段'}
                      </div>
                    </div>
                  ))}
                </div>
              </div>
            )}
          </div>
        )}
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
              className={`grid gap-2 rounded-lg border border-slate-700 bg-slate-900/60 p-3 md:grid-cols-[190px_110px_130px_1fr_auto] ${
                event.is_reversed ? 'opacity-60' : ''
              }`}
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
              <div className="text-sm text-slate-300">
                {eventDescription(event)}
                {event.is_reversed && (
                  <span className="ml-2 rounded border border-slate-600 px-1.5 py-0.5 text-[11px] text-slate-400">
                    已冲销
                  </span>
                )}
              </div>
              <div className="flex justify-end">
                {event.event_type !== 'REVERSAL' && !event.is_reversed ? (
                  <button
                    type="button"
                    onClick={() => void previewReversal(event)}
                    disabled={Boolean(reversingId)}
                    className="flex items-center gap-1 rounded border border-slate-600 px-2 py-1 text-xs text-slate-400 hover:border-amber-500/50 hover:text-amber-300 disabled:opacity-40"
                  >
                    <Undo2 className="h-3.5 w-3.5" />
                    {reversingId === event.transaction_id ? '检查中…' : '冲销'}
                  </button>
                ) : null}
              </div>
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
