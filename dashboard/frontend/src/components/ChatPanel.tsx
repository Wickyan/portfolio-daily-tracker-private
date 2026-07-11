import { useState, useRef, useEffect, useCallback } from 'react'
import { Send, Image, Loader2, X, Upload, MessageSquare, PlusCircle, History, RotateCcw } from 'lucide-react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { useQueryClient } from '@tanstack/react-query'
import { useAppStore } from '@/store'
import { chatService, portfolioService } from '@/services'
import { getApiErrorMessage } from '@/services/chat'
import ConversationHistory from './ConversationHistory'
import type { ChatMessage, PendingAction, PendingChange } from '@/types'

const TERMINAL_PENDING_STATUSES = new Set(['confirmed', 'cancelled', 'rolled_back', 'expired', 'superseded'])

function parseChineseOrdinal(value: string): number | null {
  if (/^\d+$/.test(value)) return Number(value)
  const map: Record<string, number> = { 一: 1, 二: 2, 两: 2, 三: 3, 四: 4, 五: 5, 六: 6, 七: 7, 八: 8, 九: 9, 十: 10 }
  if (value in map) return map[value]
  if (/^十[一二三四五六七八九]$/.test(value)) return 10 + (map[value[1]] || 0)
  if (/^[二三四五六七八九]十$/.test(value)) return (map[value[0]] || 0) * 10
  if (/^[二三四五六七八九]十[一二三四五六七八九]$/.test(value)) {
    return (map[value[0]] || 0) * 10 + (map[value[2]] || 0)
  }
  return null
}

function looksLikePendingRevision(pending: PendingAction, text: string): boolean {
  const value = text.trim()
  const missing = new Set(pending.missing_fields || [])
  if (!value || pending.requires_confirmation) return false
  if (missing.has('account') && /^(?:账户|券商|分组)?\s*(?:改成|改为|是|为|[:：])?\s*(?:长桥|哈富|IBKR|IB|盈透|尊嘉|华盛通|银河|富途|老虎|雪盈|中信证券)$/i.test(value)) return true
  if (missing.has('code') && /^(?:代码|code)?\s*[:：]?\s*(?:[A-Za-z]{1,8}|\d{4,6})$/i.test(value)) return true
  if (missing.has('currency') && /^(?:币种)?\s*(?:改成|改为|是|为|[:：])?\s*(?:人民币|人民币元|元|CNY|RMB|港币|港元|HKD|美元|美金|美刀|USD)$/i.test(value)) return true
  if (missing.has('quantity') && /^(?:数量)?\s*(?:改成|改为|是|为|[:：])?\s*[+-]?[\d,.]+\s*(?:股|份|个)?$/i.test(value)) return true
  if ((missing.has('cost_price') || missing.has('cost_price_non_negative')) && /^(?:成本价|均价|成交价)?\s*(?:改成|改为|是|为|[:：])?\s*[+-]?[\d,.]+$/i.test(value)) return true
  return /^(?:改成|改为|修改为|修改成|不是).+/.test(value)
}


function parseTargetedConfirmOrdinal(command: string): number | null {
  const match = command.match(/^(?:再)?确认(?:第)?([一二两三四五六七八九十\d]+)条$/)
  if (!match) return null
  const ordinal = parseChineseOrdinal(match[1])
  return ordinal && ordinal > 0 ? ordinal : null
}

function PendingActionCard({
  pending,
  onRevise,
  onConfirm,
  onCancel,
  onRollback,
}: {
  pending: PendingAction
  onRevise: (message: string) => Promise<void>
  onConfirm: () => Promise<void>
  onCancel: () => Promise<void>
  onRollback?: () => Promise<void>
}) {
  const [reviseText, setReviseText] = useState('')
  const [isWorking, setIsWorking] = useState(false)
  const changes: PendingChange[] = pending.changes?.length ? pending.changes : [{}]
  const change: PendingChange = changes[0]

  const field = (label: string, value?: string | number | null) => {
    const displayValue = typeof value === 'number'
      ? new Intl.NumberFormat('zh-CN', { maximumFractionDigits: 8 }).format(value)
      : value
    return (
      <div className="flex justify-between gap-4 border-b border-slate-700/60 py-1.5 text-sm">
        <span className="text-slate-400">{label}</span>
        <span className="text-right font-medium">{displayValue === undefined || displayValue === null || displayValue === '' ? '缺失' : displayValue}</span>
      </div>
    )
  }

  const run = async (fn: () => Promise<void>) => {
    setIsWorking(true)
    try {
      await fn()
    } finally {
      setIsWorking(false)
    }
  }

  return (
    <div className="space-y-3 rounded-lg border border-primary-500/40 bg-slate-800 p-4">
      <div className="font-semibold text-primary-300">{pending.summary || '待确认记账信息'}</div>
      <div className="space-y-2">
        {changes.map((item, index) => {
          const childAction = item.action_type || pending.action_type || ''
          const isCashAction = ['deposit', 'withdraw', 'set_cash'].includes(childAction)
          const actionLabel: Record<string, string> = {
            deposit: '增加现金',
            withdraw: '减少现金',
            set_cash: '设置现金余额',
            fx_exchange: '换汇',
            sell: '卖出',
            add_or_update: '买入/新增',
          }
          const sectionTitle = pending.action_type === 'fx_exchange'
            ? (childAction === 'withdraw' ? '换出' : '换入')
            : (changes.length > 1 ? `第${index + 1}条` : '')
          return (
            <div key={`${childAction}-${item.account || ''}-${item.currency || ''}-${index}`} className="rounded bg-slate-900/60 px-3 py-2">
              {sectionTitle && <div className="mb-1 text-sm font-semibold text-primary-300">{sectionTitle}</div>}
              {field('账户分组', item.account)}
              {isCashAction ? (
                <>
                  {field('币种', item.currency)}
                  {field('金额', item.amount)}
                  {field('操作', actionLabel[childAction] || childAction)}
                </>
              ) : (
                <>
                  {field('标的', item.name)}
                  {field('代码', item.code)}
                  {field('币种', item.currency)}
                  {field('类型', item.asset_type)}
                  {field('操作', actionLabel[childAction] || childAction)}
                  {field('数量', item.quantity)}
                  {field('成本价', item.cost_price)}
                  {field('总成本', item.total_cost)}
                  {field('手续费', item.fee ?? '未提供')}
                </>
              )}
            </div>
          )
        })}
      </div>
      {pending.missing_fields?.length > 0 && (
        <div className="text-sm text-amber-300">
          缺少/阻止写入：{pending.missing_fields.map((item) => ({
            account: '账户',
            amount: '金额',
            positive_amount: '金额必须大于0',
            amount_non_negative: '余额不能为负',
            positive_quantity: '数量必须大于0',
            cost_price_non_negative: '成本价不能为负',
            unsupported_currency: '暂仅支持CNY/USD/HKD',
            currency_conflict: '代码与币种不匹配',
            invalid_number: '数字格式无效',
            multiple_operations: '请拆分不同类型的操作',
            existing_position: '未找到对应持仓',
            available_quantity: '卖出数量超过现有持仓',
            available_cash: '可用现金不足',
            distinct_currencies: '换出和换入币种必须不同',
            'source_amount/currency': '换出金额和币种',
            'target_amount/currency': '换入金额和币种',
          }[item] || item)).join('、')}
        </div>
      )}
      {pending.warnings?.length > 0 && (
        <div className="space-y-1 text-sm text-slate-300">
          {pending.warnings.map((warning, index) => (
            <div key={index}>- {warning}</div>
          ))}
        </div>
      )}
      {pending.instrument_candidates && pending.instrument_candidates.length > 1 && !change.code && (
        <div className="space-y-2">
          <div className="text-sm text-slate-300">搜索候选：</div>
          <div className="flex flex-wrap gap-2">
            {pending.instrument_candidates.slice(0, 5).map((candidate) => (
              <button
                key={`${candidate.code}-${candidate.currency}`}
                onClick={() => run(async () => {
                  await onRevise(`代码${candidate.code}`)
                })}
                disabled={isWorking}
                className="rounded border border-slate-600 bg-slate-700 px-2.5 py-1.5 text-left text-xs hover:border-primary-500 hover:bg-slate-600 disabled:opacity-50"
              >
                <span className="font-semibold">{candidate.code}</span> {candidate.name}
              </button>
            ))}
          </div>
        </div>
      )}
      {pending.status === 'confirmed' ? (
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div className="text-sm text-green-300">写入成功，Portfolio 已刷新。</div>
          {pending.operation_id && onRollback && (
            <button
              onClick={() => run(async () => {
                const target = pending.action_type === 'fx_exchange'
                  ? '这笔换汇'
                  : (change.name || change.code || '这条记录')
                if (!window.confirm(`确定撤回${target}的这次写入吗？后续其他记录会保留。`)) return
                await onRollback()
              })}
              disabled={isWorking}
              className="flex items-center gap-1.5 rounded bg-amber-600/90 px-3 py-1.5 text-sm text-white hover:bg-amber-500 disabled:opacity-50"
            >
              <RotateCcw className="h-4 w-4" />
              撤回此条
            </button>
          )}
        </div>
      ) : pending.status === 'rolled_back' ? (
        <div className="text-sm text-amber-300">该次写入已撤回，Portfolio 已恢复。</div>
      ) : pending.status === 'superseded' ? (
        <div className="text-sm text-sky-300">原卡片已被修改后的新确认卡替代，不能再确认这一版。</div>
      ) : pending.status === 'cancelled' ? (
        <div className="text-sm text-slate-400">已取消。</div>
      ) : pending.status === 'expired' ? (
        <div className="text-sm text-amber-300">该确认卡已过期，请重新提交原始记账信息。</div>
      ) : (
        <div className="space-y-2">
          <input
            value={reviseText}
            onChange={(e) => setReviseText(e.target.value)}
            placeholder="补充/修改信息"
            className="w-full rounded bg-slate-700 px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-primary-500"
          />
          <div className="flex flex-wrap gap-2">
            <button
              onClick={() => run(async () => {
                if (!reviseText.trim()) return
                await onRevise(reviseText.trim())
                setReviseText('')
              })}
              disabled={isWorking || !reviseText.trim()}
              className="rounded bg-slate-700 px-3 py-1.5 text-sm hover:bg-slate-600 disabled:opacity-50"
            >
              修改
            </button>
            <button
              onClick={() => run(async () => {
                if (reviseText.trim()) {
                  await onRevise(reviseText.trim())
                  setReviseText('')
                  return
                }
                await onConfirm()
              })}
              disabled={isWorking || (!pending.requires_confirmation && !reviseText.trim())}
              className="rounded bg-primary-600 px-3 py-1.5 text-sm hover:bg-primary-500 disabled:opacity-50"
            >
              {reviseText.trim() ? '生成新确认卡' : '确认写入'}
            </button>
            <button
              onClick={() => run(onCancel)}
              disabled={isWorking}
              className="rounded bg-slate-700 px-3 py-1.5 text-sm hover:bg-slate-600 disabled:opacity-50"
            >
              取消
            </button>
          </div>
        </div>
      )}
    </div>
  )
}

export default function ChatPanel() {
  const [input, setInput] = useState('')
  const [selectedImages, setSelectedImages] = useState<{file: File, preview: string}[]>([])
  const [isDragging, setIsDragging] = useState(false)
  const [showHistory, setShowHistory] = useState(false)
  const fileInputRef = useRef<HTMLInputElement>(null)
  const messagesEndRef = useRef<HTMLDivElement>(null)
  const dropZoneRef = useRef<HTMLDivElement>(null)
  const queryClient = useQueryClient()

  const {
    messages,
    isLoading,
    addMessage,
    setMessages,
    setLoading,
    clearMessages,
  } = useAppStore()

  // 自动滚动到底部
  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages])

  // 页面刷新后恢复当前记账记录，并向后端校准pending状态。
  useEffect(() => {
    const pendingEntries = messages
      .map((message, index) => ({ index, pending: message.pendingAction }))
      .filter(({ pending }) => Boolean(pending?.pending_id))

    if (pendingEntries.length === 0) return

    let cancelled = false
    void Promise.all(
      pendingEntries.map(async ({ index, pending }) => {
        try {
          const latest = await portfolioService.getPending(pending!.pending_id!)
          return { index, latest }
        } catch {
          return null
        }
      }),
    ).then((results) => {
      if (cancelled) return
      const updates = new Map(
        results.filter(Boolean).map((result) => [result!.index, result!.latest]),
      )
      if (updates.size === 0) return
      setMessages(messages.map((message, index) =>
        updates.has(index) ? { ...message, pendingAction: updates.get(index) } : message,
      ))
    })

    return () => { cancelled = true }
    // 只在组件首次挂载时校准，避免每次卡片更新都重复请求。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // 处理图片选择（文件或拖拽）
  const handleImageFile = useCallback((file: File) => {
    if (!file.type.startsWith('image/')) {
      return
    }

    // 创建预览
    const reader = new FileReader()
    reader.onload = (e) => {
      setSelectedImages(prev => [...prev, { file, preview: e.target?.result as string }])
    }
    reader.readAsDataURL(file)
  }, [])

  // 清除单张图片
  const clearImage = useCallback((index: number) => {
    setSelectedImages(prev => prev.filter((_, i) => i !== index))
  }, [])

  // 清除所有图片
  const clearAllImages = useCallback(() => {
    setSelectedImages([])
  }, [])

  // 拖拽事件处理
  const handleDragEnter = useCallback((e: React.DragEvent) => {
    e.preventDefault()
    e.stopPropagation()
    setIsDragging(true)
  }, [])

  const handleDragLeave = useCallback((e: React.DragEvent) => {
    e.preventDefault()
    e.stopPropagation()
    // 检查是否真的离开了拖拽区域
    if (e.currentTarget.contains(e.relatedTarget as Node)) {
      return
    }
    setIsDragging(false)
  }, [])

  const handleDragOver = useCallback((e: React.DragEvent) => {
    e.preventDefault()
    e.stopPropagation()
  }, [])

  const handleDrop = useCallback((e: React.DragEvent) => {
    e.preventDefault()
    e.stopPropagation()
    setIsDragging(false)

    const files = e.dataTransfer.files
    if (files && files.length > 0) {
      Array.from(files).forEach(file => {
        if (file.type.startsWith('image/')) {
          handleImageFile(file)
        }
      })
    }
  }, [handleImageFile])

  // 粘贴事件处理
  const handlePaste = useCallback((e: React.ClipboardEvent) => {
    const items = e.clipboardData.items
    for (let i = 0; i < items.length; i++) {
      if (items[i].type.startsWith('image/')) {
        const file = items[i].getAsFile()
        if (file) {
          handleImageFile(file)
          e.preventDefault()
          break
        }
      }
    }
  }, [handleImageFile])

  // 清空当前记账记录
  const handleNewConversation = async () => {
    if (window.confirm('确定要清空当前记账记录吗？已写入Portfolio的数据不会受影响。')) {
      try {
        await chatService.clearHistory()
        clearMessages()
      } catch (error) {
        console.error('Failed to start new conversation:', error)
        alert('清空记账记录失败')
      }
    }
  }

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!input.trim() && selectedImages.length === 0) return
    if (isLoading) return

    const currentImages = [...selectedImages]
    const userMessage = input.trim()
    const normalizedCommand = userMessage.replace(/\s+/g, '')
    const confirmCommands = new Set(['确认', '确定', '确认写入', '写入', '保存', '录入', '提交'])
    const cancelCommands = new Set(['取消', '取消写入', '不写了'])
    const targetedConfirmOrdinal = parseTargetedConfirmOrdinal(normalizedCommand)
    const isRollbackCommand = /撤回|撤销|回滚|undo/i.test(normalizedCommand)

    const activePendingEntries = messages
      .map((message, index) => ({ message, index, pending: message.pendingAction }))
      .filter(({ pending }) => (
        Boolean(pending?.pending_id)
        && !TERMINAL_PENDING_STATUSES.has(pending?.status || 'pending')
      ))
    const latestPendingEntry = activePendingEntries.length > 0
      ? activePendingEntries[activePendingEntries.length - 1]
      : null

    // 立即清空输入和图片
    setInput('')
    clearAllImages()

    // “撤回/撤销/回滚”直接撤回最近一次尚未撤回的真实写入。
    if (currentImages.length === 0 && isRollbackCommand) {
      const userEntry: ChatMessage = { role: 'user', content: userMessage }
      setLoading(true)
      try {
        const result = await portfolioService.rollbackLatest()
        let targetIndex = messages.findIndex(
          (message) => message.pendingAction?.operation_id === result.rolled_back_operation_id,
        )
        if (targetIndex < 0) {
          for (let i = messages.length - 1; i >= 0; i -= 1) {
            if (messages[i].pendingAction?.status === 'confirmed') {
              targetIndex = i
              break
            }
          }
        }

        const nextMessages = messages.map((message, index) => {
          if (index !== targetIndex || !message.pendingAction) return message
          return {
            ...message,
            pendingAction: {
              ...message.pendingAction,
              status: 'rolled_back',
              requires_confirmation: false,
              operation_id: result.rolled_back_operation_id,
            },
          }
        })
        const description = result.description ? `：${result.description}` : ''
        setMessages([
          ...nextMessages,
          userEntry,
          { role: 'assistant', content: `已撤回最近一次写入${description}。Portfolio已恢复。` },
        ])
        queryClient.invalidateQueries({ queryKey: ['portfolio'] })
        queryClient.invalidateQueries({ queryKey: ['portfolio', 'operations'] })
        await queryClient.refetchQueries({ queryKey: ['portfolio'], type: 'active' })
      } catch (error) {
        setMessages([
          ...messages,
          userEntry,
          { role: 'assistant', content: `撤回失败：${getApiErrorMessage(error)}` },
        ])
      } finally {
        setLoading(false)
      }
      return
    }

    // “确认第二条/再确认第一条”可精确选择未确认卡片；普通“确认”仍操作最新一条。
    const isConfirmCommand = confirmCommands.has(normalizedCommand) || targetedConfirmOrdinal !== null
    if (currentImages.length === 0 && (isConfirmCommand || cancelCommands.has(normalizedCommand))) {
      const userEntry: ChatMessage = { role: 'user', content: userMessage }
      let targetEntry = latestPendingEntry
      if (targetedConfirmOrdinal !== null) {
        targetEntry = activePendingEntries[targetedConfirmOrdinal - 1] || null
      }

      if (!targetEntry?.pending?.pending_id) {
        const message = targetedConfirmOrdinal !== null
          ? `当前只有${activePendingEntries.length}条待确认记录，找不到第${targetedConfirmOrdinal}条。`
          : '当前没有待确认记录，请先提交一条交易或持仓信息。'
        useAppStore.setState((state) => ({
          messages: [...state.messages, userEntry, { role: 'assistant', content: message }],
        }))
        return
      }

      const targetPending = targetEntry.pending
      const targetPendingId = targetPending.pending_id!
      if (isConfirmCommand && !targetPending.requires_confirmation) {
        const missing = targetPending.missing_fields?.join('、') || '必要字段'
        useAppStore.setState((state) => ({
          messages: [
            ...state.messages,
            userEntry,
            { role: 'assistant', content: `当前记录还不能写入，仍缺少：${missing}。请先补充或修改。` },
          ],
        }))
        return
      }

      setLoading(true)
      try {
        let operationId = targetPending.operation_id
        if (cancelCommands.has(normalizedCommand)) {
          await portfolioService.aiCancel(targetPendingId)
        } else {
          const result = await portfolioService.aiConfirm(targetPendingId)
          operationId = result.operation_id
          queryClient.invalidateQueries({ queryKey: ['portfolio'] })
          queryClient.invalidateQueries({ queryKey: ['portfolio', 'operations'] })
          await queryClient.refetchQueries({ queryKey: ['portfolio'], type: 'active' })
        }

        useAppStore.setState((state) => ({
          messages: [
            ...state.messages.map((message): ChatMessage => {
              const currentPending = message.pendingAction
              if (!currentPending || currentPending.pending_id !== targetPendingId) return message
              return {
                ...message,
                pendingAction: {
                  ...currentPending,
                  status: cancelCommands.has(normalizedCommand) ? 'cancelled' : 'confirmed',
                  requires_confirmation: false,
                  operation_id: operationId,
                },
              }
            }),
            userEntry,
          ],
        }))
      } catch (error) {
        useAppStore.setState((state) => ({
          messages: [
            ...state.messages,
            userEntry,
            { role: 'assistant', content: `操作失败：${getApiErrorMessage(error)}` },
          ],
        }))
      } finally {
        setLoading(false)
      }
      return
    }

    // A concise reply to the latest incomplete card is treated as a field revision.
    if (
      currentImages.length === 0
      && latestPendingEntry?.pending?.pending_id
      && looksLikePendingRevision(latestPendingEntry.pending, userMessage)
    ) {
      const originalPendingId = latestPendingEntry.pending.pending_id
      setLoading(true)
      try {
        const updated = await portfolioService.aiRevise(originalPendingId, userMessage)
        useAppStore.setState((state) => ({
          messages: [
            ...state.messages,
            { role: 'user', content: userMessage },
          ],
        }))
        appendRevisedPendingMessage(originalPendingId, updated)
      } catch (error) {
        useAppStore.setState((state) => ({
          messages: [
            ...state.messages,
            { role: 'user', content: userMessage },
            { role: 'assistant', content: `补充失败：${getApiErrorMessage(error)}` },
          ],
        }))
      } finally {
        setLoading(false)
      }
      return
    }

    // 添加用户消息
    addMessage({
      role: 'user',
      content: userMessage,
      images: currentImages.map(img => img.preview), // 保存多图预览
    })

    setLoading(true)

    try {
      if (currentImages.length > 0) {
        // 带图片的消息：先让视觉模型生成文字，再只生成 preview，不直接写 portfolio。
        const response = await chatService.sendMessageWithImage(
          userMessage,
          currentImages.map(c => c.file),
          true
        )

        const preview = await portfolioService.aiPreview(`${userMessage}\n${response.response}`, 'image_text')
        if (preview.intent !== 'chat_only' && preview.pending_id) {
          addMessage({
            role: 'assistant',
            content: '已生成待确认记账预览，当前不会写入portfolio。',
            pendingAction: preview,
          })
        } else {
          addMessage({
            role: 'assistant',
            content: '未能从图片中生成可确认的记账卡。请补充账户、标的/代码、数量、成本价或现金币种与金额后重新提交。',
          })
        }

        // 图片模型仅负责提取记账信息；本页面不展示投资建议或普通聊天内容。

        // 如果有持仓导入，刷新持仓数据
        if (response.imported_positions && response.imported_positions > 0) {
          queryClient.invalidateQueries({ queryKey: ['portfolio'] })
        }
      } else {
        const preview = await portfolioService.aiPreview(userMessage, 'text')
        if (preview.intent !== 'chat_only' && preview.pending_id) {
          addMessage({
            role: 'assistant',
            content: '已生成待确认记账预览，当前不会写入portfolio。',
            pendingAction: preview,
          })
          return
        }

        // 本页面是专用记账入口，不再把未识别内容转发到通用聊天模型。
        const reason = preview.summary && !preview.summary.includes('普通聊天')
          ? `：${preview.summary}`
          : ''
        addMessage({
          role: 'assistant',
          content: `未生成记账确认卡${reason}。本页面只处理已经发生的持仓、现金和换汇记录，请补充明确的账户、标的、数量、价格或币种金额。`,
        })
        return
      }
    } catch (error) {
      console.error('Chat error:', error)
      addMessage({
        role: 'assistant',
        content: `抱歉，请求失败：${getApiErrorMessage(error)}`,
      })
    } finally {
      setLoading(false)
    }
  }

  const handleImageSelect = (e: React.ChangeEvent<HTMLInputElement>) => {
    const files = e.target.files
    if (files) {
      Array.from(files).forEach(file => {
        if (file.type.startsWith('image/')) handleImageFile(file)
      })
    }
    // reset input
    if (e.target) e.target.value = ''
  }

  const updatePendingMessage = (pendingId: string, pending: PendingAction) => {
    useAppStore.setState((state) => ({
      messages: state.messages.map((message) =>
        message.pendingAction?.pending_id === pendingId
          ? { ...message, pendingAction: pending }
          : message,
      ),
    }))
  }

  const appendRevisedPendingMessage = (originalPendingId: string, pending: PendingAction) => {
    useAppStore.setState((state) => ({
      messages: [
        ...state.messages.map((message) => {
          if (message.pendingAction?.pending_id !== originalPendingId) return message
          return {
            ...message,
            pendingAction: {
              ...message.pendingAction,
              status: 'superseded',
              requires_confirmation: false,
              revised_to_pending_id: pending.pending_id,
            },
          }
        }),
        {
          role: 'assistant',
          content: '已根据修改内容生成新的待确认卡片。',
          pendingAction: pending,
        },
      ],
    }))
  }

  const renderMessage = (message: ChatMessage, index: number) => {
    const isUser = message.role === 'user'

    return (
      <div
        key={index}
        className={`flex ${isUser ? 'justify-end' : 'justify-start'} mb-4`}
      >
        <div
          className={`max-w-[80%] rounded-lg px-4 py-3 ${
            isUser
              ? 'bg-primary-600 text-white'
              : 'bg-slate-700 text-slate-100'
          }`}
        >
          {/* 显示图片 (旧兼容逻辑单图) */}
          {message.image && (
            <div className="mb-2">
              <img
                src={message.image}
                alt="上传的图片"
                className="max-w-full rounded-lg max-h-48 object-contain"
              />
            </div>
          )}
          {/* 显示多张图片 */}
          {message.images && message.images.length > 0 && (
            <div className={`mb-2 grid gap-2 ${message.images.length > 1 ? 'grid-cols-2' : 'grid-cols-1'}`}>
              {message.images.map((imgUrl, i) => (
                <img
                  key={i}
                  src={imgUrl}
                  alt={`上传图片 ${i+1}`}
                  className="max-w-full rounded-lg max-h-48 object-contain"
                />
              ))}
            </div>
          )}
          {message.pendingAction ? (
            <PendingActionCard
              pending={message.pendingAction}
              onRevise={async (text) => {
                if (!message.pendingAction?.pending_id) return
                const originalPendingId = message.pendingAction.pending_id
                const updated = await portfolioService.aiRevise(originalPendingId, text)
                appendRevisedPendingMessage(originalPendingId, updated)
              }}
              onConfirm={async () => {
                if (!message.pendingAction?.pending_id) return
                const result = await portfolioService.aiConfirm(message.pendingAction.pending_id)
                queryClient.invalidateQueries({ queryKey: ['portfolio'] })
                queryClient.invalidateQueries({ queryKey: ['portfolio', 'operations'] })
                await queryClient.refetchQueries({ queryKey: ['portfolio'], type: 'active' })
                updatePendingMessage(message.pendingAction.pending_id, {
                  ...message.pendingAction,
                  status: 'confirmed',
                  requires_confirmation: false,
                  operation_id: result.operation_id,
                })
              }}
              onCancel={async () => {
                if (!message.pendingAction?.pending_id) return
                await portfolioService.aiCancel(message.pendingAction.pending_id)
                updatePendingMessage(message.pendingAction.pending_id, { ...message.pendingAction, status: 'cancelled', requires_confirmation: false })
              }}
              onRollback={message.pendingAction?.operation_id ? async () => {
                const operationId = message.pendingAction!.operation_id!
                await portfolioService.rollback(operationId)
                queryClient.invalidateQueries({ queryKey: ['portfolio'] })
                queryClient.invalidateQueries({ queryKey: ['portfolio', 'operations'] })
                await queryClient.refetchQueries({ queryKey: ['portfolio'], type: 'active' })
                updatePendingMessage(message.pendingAction!.pending_id!, {
                  ...message.pendingAction!,
                  status: 'rolled_back',
                  requires_confirmation: false,
                })
              } : undefined}
            />
          ) : isUser ? (
            <p className="whitespace-pre-wrap">{message.content}</p>
          ) : (
            <div className="prose prose-invert max-w-none prose-td:border prose-td:border-slate-600 prose-th:border prose-th:border-slate-600 prose-th:bg-slate-800 prose-table:w-auto overflow-x-auto">
              <ReactMarkdown remarkPlugins={[remarkGfm]}>{message.content}</ReactMarkdown>
            </div>
          )}
        </div>
      </div>
    )
  }

  return (
    <div
      ref={dropZoneRef}
      className={`relative flex h-full min-h-0 flex-col overflow-hidden card ${
        isDragging ? 'ring-2 ring-primary-500 ring-offset-2 ring-offset-slate-900' : ''
      }`}
      onDragEnter={handleDragEnter}
      onDragLeave={handleDragLeave}
      onDragOver={handleDragOver}
      onDrop={handleDrop}
    >
      {/* 拖拽覆盖层 */}
      {isDragging && (
        <div className="absolute inset-0 bg-primary-600/20 backdrop-blur-sm z-50 flex items-center justify-center rounded-lg border-2 border-dashed border-primary-500">
          <div className="text-center">
            <Upload className="h-12 w-12 mx-auto mb-2 text-primary-400" />
            <p className="text-lg font-medium text-primary-400">释放以上传图片</p>
            <p className="text-sm text-slate-400 mt-1">支持 JPG、PNG 等图片格式</p>
          </div>
        </div>
      )}

      {/* 顶部工具栏 */}
      <div className="flex shrink-0 items-center justify-between border-b border-slate-700 px-4 py-3">
        <div className="flex items-center space-x-2">
          <MessageSquare className="h-5 w-5 text-slate-400" />
          <span className="text-sm font-medium text-slate-300">
            {messages.length > 0 ? `记账记录 (${messages.length} 条消息)` : '准备记账'}
          </span>
        </div>
        <div className="flex items-center space-x-2">
          <button
            onClick={() => setShowHistory(true)}
            className="flex items-center space-x-1.5 px-3 py-1.5 text-sm bg-slate-700 hover:bg-slate-600 rounded-lg transition-colors"
            title="查看历史记账记录"
          >
            <History className="h-4 w-4" />
            <span>历史</span>
          </button>
          <button
            onClick={handleNewConversation}
            disabled={messages.length === 0}
            className="flex items-center space-x-1.5 px-3 py-1.5 text-sm bg-primary-600 hover:bg-primary-500 disabled:bg-slate-700 disabled:text-slate-500 disabled:cursor-not-allowed rounded-lg transition-colors"
            title="清空当前记账记录"
          >
            <PlusCircle className="h-4 w-4" />
            <span>清空记录</span>
          </button>
        </div>
      </div>

      {/* 消息列表 */}
      <div className="min-h-0 flex-1 overflow-y-auto px-4 py-4 custom-scrollbar">
        {messages.length === 0 ? (
          <div className="flex h-full items-center justify-center text-slate-400">
            <div className="text-center">
              <p className="text-lg">开始记录持仓或现金变化</p>
              <p className="mt-2 text-sm">
                例如：银河增加2万元；长桥买入100股苹果，均价200美元
              </p>
              <p className="mt-4 text-xs text-slate-500">
                也可以拖拽或粘贴券商持仓截图生成待确认记账卡
              </p>
            </div>
          </div>
        ) : (
          <>
            {messages.map(renderMessage)}
            {isLoading && (
              <div className="flex justify-start mb-4">
                <div className="rounded-lg px-4 py-3 bg-slate-700">
                  <Loader2 className="h-5 w-5 animate-spin text-slate-400" />
                </div>
              </div>
            )}
          </>
        )}
        <div ref={messagesEndRef} />
      </div>

      {/* 图片预览 - 支持多张图片 */}
      {selectedImages.length > 0 && (
        <div className="px-4 py-2 border-t border-slate-700 overflow-x-auto">
          <div className="flex items-start gap-4">
            {selectedImages.map((imgObj, i) => (
              <div key={i} className="flex relative flex-col items-center flex-shrink-0">
                <div className="relative">
                  <img
                    src={imgObj.preview}
                    alt={`预览 ${i}`}
                    className="h-20 w-auto rounded-lg object-contain bg-slate-800 border border-slate-600"
                  />
                  <button
                    type="button"
                    onClick={() => clearImage(i)}
                    className="absolute -top-2 -right-2 p-1 bg-red-500 rounded-full hover:bg-red-400 transition-colors z-10"
                  >
                    <X className="h-3 w-3" />
                  </button>
                </div>
                <div className="w-full text-center mt-1 truncate text-xs text-slate-400 max-w-[100px]">
                  {imgObj.file.name}
                  <br />
                  {(imgObj.file.size / 1024).toFixed(1)} KB
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* 输入区域 */}
      <form onSubmit={handleSubmit} className="shrink-0 border-t border-slate-700 p-4">
        <div className="flex items-center gap-3">
          <input
            type="file"
            ref={fileInputRef}
            onChange={handleImageSelect}
            accept="image/*"
            multiple
            className="hidden"
          />
          <button
            type="button"
            onClick={() => fileInputRef.current?.click()}
            className="p-2 text-slate-400 hover:text-white hover:bg-slate-700 rounded-lg transition-colors"
            title="上传图片（也可以直接拖拽或粘贴）"
          >
            <Image className="h-5 w-5" />
          </button>
          <input
            type="text"
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onPaste={handlePaste}
            placeholder="输入已发生的持仓/现金/换汇记录…（支持拖拽或Ctrl+V粘贴图片）"
            className="flex-1 input-field"
            disabled={isLoading}
          />
          <button
            type="submit"
            disabled={isLoading || (!input.trim() && selectedImages.length === 0)}
            className="btn-primary disabled:opacity-50 disabled:cursor-not-allowed"
          >
            <Send className="h-5 w-5" />
          </button>
        </div>
      </form>

      {/* 历史记账记录模态框 */}
      <ConversationHistory
        isOpen={showHistory}
        onClose={() => setShowHistory(false)}
      />
    </div>
  )
}
