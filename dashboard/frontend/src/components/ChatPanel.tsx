import { useState, useRef, useEffect, useCallback } from 'react'
import { Send, Image, Loader2, X, Upload, MessageSquare, PlusCircle, History } from 'lucide-react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { useQueryClient } from '@tanstack/react-query'
import { useAppStore } from '@/store'
import { chatService, portfolioService } from '@/services'
import { getApiErrorMessage } from '@/services/chat'
import ConversationHistory from './ConversationHistory'
import type { ChatMessage, PendingAction, PendingChange } from '@/types'

function PendingActionCard({
  pending,
  onRevise,
  onConfirm,
  onCancel,
}: {
  pending: PendingAction
  onRevise: (message: string) => Promise<void>
  onConfirm: () => Promise<void>
  onCancel: () => Promise<void>
}) {
  const [reviseText, setReviseText] = useState('')
  const [isWorking, setIsWorking] = useState(false)
  const change: PendingChange = pending.changes?.[0] || {}

  const field = (label: string, value?: string | number | null) => (
    <div className="flex justify-between gap-4 border-b border-slate-700/60 py-1.5 text-sm">
      <span className="text-slate-400">{label}</span>
      <span className="text-right font-medium">{value === undefined || value === null || value === '' ? '缺失' : value}</span>
    </div>
  )

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
      <div className="rounded bg-slate-900/60 px-3 py-2">
        {field('账户分组', change.account)}
        {field('标的', change.name)}
        {field('代码', change.code)}
        {field('币种', change.currency)}
        {field('类型', change.asset_type)}
        {field('操作', pending.action_type)}
        {field('数量', change.quantity)}
        {field('成本价', change.cost_price)}
        {field('总成本', change.total_cost)}
        {field('手续费', change.fee ?? '未提供')}
      </div>
      {pending.missing_fields?.length > 0 && (
        <div className="text-sm text-amber-300">缺少：{pending.missing_fields.join('、')}</div>
      )}
      {pending.warnings?.length > 0 && (
        <div className="space-y-1 text-sm text-slate-300">
          {pending.warnings.map((warning, index) => (
            <div key={index}>- {warning}</div>
          ))}
        </div>
      )}
      {pending.status === 'confirmed' ? (
        <div className="text-sm text-green-300">写入成功，Portfolio 已刷新。</div>
      ) : pending.status === 'cancelled' ? (
        <div className="text-sm text-slate-400">已取消。</div>
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
                }
                await onConfirm()
              })}
              disabled={isWorking || (!pending.requires_confirmation && !reviseText.trim())}
              className="rounded bg-primary-600 px-3 py-1.5 text-sm hover:bg-primary-500 disabled:opacity-50"
            >
              确认写入
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
    currentResponse,
    addMessage,
    setMessages,
    setLoading,
    setCurrentResponse,
    appendToCurrentResponse,
    setSuggestions,
    setRisks,
    clearMessages,
  } = useAppStore()

  // 自动滚动到底部
  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages, currentResponse])

  // 页面刷新后恢复当前聊天，并向后端校准pending状态。
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

  // 开始新对话
  const handleNewConversation = async () => {
    if (window.confirm('确定要开始新对话吗？当前对话将被清空（但已保存到历史记录中）')) {
      try {
        await chatService.clearHistory()
        clearMessages()
      } catch (error) {
        console.error('Failed to start new conversation:', error)
        alert('开始新对话失败')
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
    const confirmCommands = new Set(['确认', '确认写入', '写入', '保存', '录入', '提交'])
    const cancelCommands = new Set(['取消', '取消写入', '不写了'])

    let latestPendingIndex = -1
    for (let i = messages.length - 1; i >= 0; i -= 1) {
      const pending = messages[i].pendingAction
      if (pending?.pending_id && pending.status !== 'confirmed' && pending.status !== 'cancelled') {
        latestPendingIndex = i
        break
      }
    }

    // 立即清空输入和图片
    setInput('')
    clearAllImages()

    // “确认/写入/取消”只操作最近一条pending，不再作为普通聊天发送。
    if (currentImages.length === 0 && (confirmCommands.has(normalizedCommand) || cancelCommands.has(normalizedCommand))) {
      const userEntry: ChatMessage = { role: 'user', content: userMessage }
      if (latestPendingIndex < 0) {
        setMessages([
          ...messages,
          userEntry,
          { role: 'assistant', content: '当前没有待确认记录，请先提交一条交易或持仓信息。' },
        ])
        return
      }

      const latestPending = messages[latestPendingIndex].pendingAction!
      if (confirmCommands.has(normalizedCommand) && !latestPending.requires_confirmation) {
        const missing = latestPending.missing_fields?.join('、') || '必要字段'
        setMessages([
          ...messages,
          userEntry,
          { role: 'assistant', content: `当前记录还不能写入，仍缺少：${missing}。请先补充或修改。` },
        ])
        return
      }

      setLoading(true)
      try {
        const nextMessages = messages.map((message, index) => {
          if (index !== latestPendingIndex || !message.pendingAction) return message
          return {
            ...message,
            pendingAction: {
              ...message.pendingAction,
              status: cancelCommands.has(normalizedCommand) ? 'cancelled' : 'confirmed',
              requires_confirmation: false,
            },
          }
        })

        if (cancelCommands.has(normalizedCommand)) {
          await portfolioService.aiCancel(latestPending.pending_id!)
        } else {
          await portfolioService.aiConfirm(latestPending.pending_id!)
          queryClient.invalidateQueries({ queryKey: ['portfolio'] })
          queryClient.invalidateQueries({ queryKey: ['portfolio', 'operations'] })
          await queryClient.refetchQueries({ queryKey: ['portfolio'], type: 'active' })
        }
        setMessages([...nextMessages, userEntry])
      } catch (error) {
        setMessages([
          ...messages,
          userEntry,
          { role: 'assistant', content: `操作失败：${getApiErrorMessage(error)}` },
        ])
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
    setCurrentResponse('')

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
            content: response.response,
          })
        }

        // 更新建议和风险
        if (response.suggestions) {
          setSuggestions(response.suggestions)
        }
        if (response.risks) {
          setRisks(response.risks)
        }

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

        // 普通文本消息默认流式返回
        let streamedResponse = ''
        await chatService.streamMessage(
          userMessage,
          (chunk) => {
            streamedResponse += chunk
            appendToCurrentResponse(chunk)
          },
          () => {}
        )

        addMessage({
          role: 'assistant',
          content: streamedResponse,
        })
        setCurrentResponse('')
        setLoading(false)

        // 在回复已展示后，异步提取建议/风险/记忆
        void chatService.extractResponse(userMessage, streamedResponse)
          .then((result) => {
            if (result.suggestions) {
              setSuggestions(result.suggestions)
            }
            if (result.risks) {
              setRisks(result.risks)
            }
            if (result.imported_positions && result.imported_positions > 0) {
              queryClient.invalidateQueries({ queryKey: ['portfolio'] })
            }
          })
          .catch((extractError) => {
            console.error('Extraction error:', extractError)
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
      setCurrentResponse('')
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

  const updatePendingMessage = (index: number, pending: PendingAction) => {
    setMessages(messages.map((msg, i) => i === index ? { ...msg, pendingAction: pending } : msg))
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
                const updated = await portfolioService.aiRevise(message.pendingAction.pending_id, text)
                updatePendingMessage(index, updated)
              }}
              onConfirm={async () => {
                if (!message.pendingAction?.pending_id) return
                await portfolioService.aiConfirm(message.pendingAction.pending_id)
                queryClient.invalidateQueries({ queryKey: ['portfolio'] })
                await queryClient.refetchQueries({ queryKey: ['portfolio'], type: 'active' })
                updatePendingMessage(index, { ...message.pendingAction, status: 'confirmed', requires_confirmation: false })
              }}
              onCancel={async () => {
                if (!message.pendingAction?.pending_id) return
                await portfolioService.aiCancel(message.pendingAction.pending_id)
                updatePendingMessage(index, { ...message.pendingAction, status: 'cancelled', requires_confirmation: false })
              }}
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
      className={`flex h-full flex-col card relative ${
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
      <div className="flex items-center justify-between px-4 py-3 border-b border-slate-700">
        <div className="flex items-center space-x-2">
          <MessageSquare className="h-5 w-5 text-slate-400" />
          <span className="text-sm font-medium text-slate-300">
            {messages.length > 0 ? `对话中 (${messages.length} 条消息)` : '准备开始对话'}
          </span>
        </div>
        <div className="flex items-center space-x-2">
          <button
            onClick={() => setShowHistory(true)}
            className="flex items-center space-x-1.5 px-3 py-1.5 text-sm bg-slate-700 hover:bg-slate-600 rounded-lg transition-colors"
            title="查看历史对话"
          >
            <History className="h-4 w-4" />
            <span>历史</span>
          </button>
          <button
            onClick={handleNewConversation}
            disabled={messages.length === 0}
            className="flex items-center space-x-1.5 px-3 py-1.5 text-sm bg-primary-600 hover:bg-primary-500 disabled:bg-slate-700 disabled:text-slate-500 disabled:cursor-not-allowed rounded-lg transition-colors"
            title="开始新对话"
          >
            <PlusCircle className="h-4 w-4" />
            <span>新对话</span>
          </button>
        </div>
      </div>

      {/* 消息列表 */}
      <div className="flex-1 overflow-y-auto px-4 py-4">
        {messages.length === 0 ? (
          <div className="flex h-full items-center justify-center text-slate-400">
            <div className="text-center">
              <p className="text-lg">开始与交易助手对话</p>
              <p className="mt-2 text-sm">
                我可以帮你分析市场、管理持仓、提供交易建议
              </p>
              <p className="mt-4 text-xs text-slate-500">
                提示：可以直接拖拽图片到这里上传
              </p>
            </div>
          </div>
        ) : (
          <>
            {messages.map(renderMessage)}
            {/* 流式回复 */}
            {currentResponse && (
              <div className="flex justify-start mb-4">
                <div className="max-w-[80%] rounded-lg px-4 py-3 bg-slate-700 text-slate-100">
                  <div className="prose prose-invert max-w-none prose-td:border prose-td:border-slate-600 prose-th:border prose-th:border-slate-600 prose-th:bg-slate-800 prose-table:w-auto overflow-x-auto">
                    <ReactMarkdown remarkPlugins={[remarkGfm]}>{currentResponse}</ReactMarkdown>
                  </div>
                </div>
              </div>
            )}
            {isLoading && !currentResponse && (
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
      <form onSubmit={handleSubmit} className="border-t border-slate-700 p-4">
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
            placeholder="输入消息...（可拖拽或 Ctrl+V 粘贴图片）"
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

      {/* 历史对话模态框 */}
      <ConversationHistory
        isOpen={showHistory}
        onClose={() => setShowHistory(false)}
      />
    </div>
  )
}
