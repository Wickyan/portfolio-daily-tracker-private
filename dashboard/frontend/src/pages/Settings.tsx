import { useState, useEffect } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { Settings as SettingsIcon, Server, Cpu, Save, DollarSign, Loader2, KeyRound } from 'lucide-react'

interface ModelInfo {
  id: string
  name: string
  description: string
  supports_vision: boolean
}

interface APIGroupInfo {
  name: string
  description: string
  models: ModelInfo[]
}

interface ConfigResponse {
  current_api_group: string
  current_model: string
  api_groups: { [key: string]: APIGroupInfo }
  available_models: ModelInfo[]
}

interface LLMConfigResponse {
  api_group: string
  base_url: string
  model: string
  api_key_configured: boolean
  api_key_masked: string
}

export default function Settings() {
  const queryClient = useQueryClient()
  const [apiUrl] = useState('http://localhost:8000')
  const [selectedModel, setSelectedModel] = useState('')
  const [selectedApiGroup, setSelectedApiGroup] = useState('')
  const [cash, setCash] = useState(50000)
  const [saveMessage, setSaveMessage] = useState('')
  const [llmMessage, setLlmMessage] = useState('')
  const [llmApiGroup, setLlmApiGroup] = useState('deepseek')
  const [llmBaseUrl, setLlmBaseUrl] = useState('https://api.deepseek.com/v1')
  const [llmApiKey, setLlmApiKey] = useState('')
  const [llmModel, setLlmModel] = useState('deepseek-chat')

  // 获取配置
  const { data: config, isLoading } = useQuery<ConfigResponse>({
    queryKey: ['settings'],
    queryFn: async () => {
      const res = await fetch('/api/settings/config')
      return res.json()
    }
  })

  const { data: llmConfig } = useQuery<LLMConfigResponse>({
    queryKey: ['settings', 'llm'],
    queryFn: async () => {
      const res = await fetch('/api/settings/llm')
      if (!res.ok) throw new Error('获取 LLM 配置失败')
      return res.json()
    }
  })

  // 获取持仓以获取当前现金
  const { data: portfolio } = useQuery({
    queryKey: ['portfolio'],
    queryFn: async () => {
      const res = await fetch('/api/portfolio')
      return res.json()
    }
  })

  // 初始化状态
  useEffect(() => {
    if (config) {
      setSelectedModel(config.current_model)
      setSelectedApiGroup(config.current_api_group)
    }
  }, [config])

  useEffect(() => {
    if (portfolio?.cash !== undefined) {
      setCash(portfolio.cash)
    }
  }, [portfolio])

  useEffect(() => {
    if (llmConfig) {
      setLlmApiGroup(llmConfig.api_group || 'deepseek')
      setLlmBaseUrl(llmConfig.base_url || 'https://api.deepseek.com/v1')
      setLlmModel(llmConfig.model || 'deepseek-chat')
      setLlmApiKey('')
    }
  }, [llmConfig])

  // 当API组改变时，重置模型选择为该组的第一个模型
  useEffect(() => {
    if (config && selectedApiGroup) {
      const group = config.api_groups[selectedApiGroup]
      if (group?.models.length > 0) {
        // 如果当前选择的模型不在新组中，选择新组的第一个模型
        const modelExists = group.models.some(m => m.id === selectedModel)
        if (!modelExists) {
          setSelectedModel(group.models[0].id)
        }
      }
    }
  }, [selectedApiGroup, config])

  // 切换API组
  const switchApiGroupMutation = useMutation({
    mutationFn: async (groupName: string) => {
      const res = await fetch('/api/settings/api-group', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ group_name: groupName })
      })
      return res.json()
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['settings'] })
      setSaveMessage('API组切换成功！已自动重载配置')
      setTimeout(() => setSaveMessage(''), 3000)
    }
  })

  // 切换模型
  const switchModelMutation = useMutation({
    mutationFn: async (modelId: string) => {
      const res = await fetch('/api/settings/model', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ model_id: modelId })
      })
      return res.json()
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['settings'] })
      setSaveMessage('模型切换成功！已自动重载配置')
      setTimeout(() => setSaveMessage(''), 3000)
    }
  })

  // 更新现金
  const updateCashMutation = useMutation({
    mutationFn: async (newCash: number) => {
      const res = await fetch('/api/settings/cash', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ cash: newCash })
      })
      return res.json()
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['portfolio'] })
      setSaveMessage('现金余额更新成功！')
      setTimeout(() => setSaveMessage(''), 3000)
    }
  })

  const testLlmMutation = useMutation({
    mutationFn: async () => {
      const res = await fetch('/api/settings/llm/test', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          api_group: llmApiGroup,
          base_url: llmBaseUrl,
          api_key: llmApiKey,
          model: llmModel
        })
      })
      const data = await res.json()
      if (!res.ok || !data.ok) {
        throw new Error(data.message || '测试连接失败')
      }
      return data
    },
    onSuccess: (data) => {
      setLlmMessage(data.message || '连接成功')
      setTimeout(() => setLlmMessage(''), 5000)
    },
    onError: (error) => {
      setLlmMessage(error instanceof Error ? error.message : '测试连接失败')
      setTimeout(() => setLlmMessage(''), 5000)
    }
  })

  const saveLlmMutation = useMutation({
    mutationFn: async () => {
      const res = await fetch('/api/settings/llm', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          api_group: llmApiGroup,
          base_url: llmBaseUrl,
          api_key: llmApiKey,
          model: llmModel
        })
      })
      const data = await res.json()
      if (!res.ok) {
        throw new Error(data.detail || '保存配置失败')
      }
      return data
    },
    onSuccess: () => {
      setLlmApiKey('')
      queryClient.invalidateQueries({ queryKey: ['settings'] })
      queryClient.invalidateQueries({ queryKey: ['settings', 'llm'] })
      setLlmMessage('DeepSeek API 配置已保存并热重载')
      setTimeout(() => setLlmMessage(''), 5000)
    },
    onError: (error) => {
      setLlmMessage(error instanceof Error ? error.message : '保存配置失败')
      setTimeout(() => setLlmMessage(''), 5000)
    }
  })

  const handleSave = () => {
    if (selectedApiGroup !== config?.current_api_group) {
      switchApiGroupMutation.mutate(selectedApiGroup)
    }
    if (selectedModel !== config?.current_model) {
      switchModelMutation.mutate(selectedModel)
    }
    if (cash !== portfolio?.cash) {
      updateCashMutation.mutate(cash)
    }
  }

  if (isLoading) {
    return (
      <div className="flex items-center justify-center h-full">
        <Loader2 className="h-8 w-8 animate-spin text-primary-500" />
      </div>
    )
  }

  const currentGroup = config?.api_groups[selectedApiGroup]
  const availableModels = currentGroup?.models || config?.available_models || []

  return (
    <div className="p-6 space-y-6">
      <h1 className="text-2xl font-bold flex items-center">
        <SettingsIcon className="h-6 w-6 mr-2" />
        设置
      </h1>

      {/* 保存消息 */}
      {saveMessage && (
        <div className="bg-green-500/10 border border-green-500 rounded-lg p-4 text-green-400">
          {saveMessage}
        </div>
      )}

      {/* API 设置 */}
      <div className="bg-slate-800 rounded-lg p-6">
        <h2 className="text-xl font-semibold flex items-center mb-4">
          <Server className="h-5 w-5 mr-2" />
          API 设置
        </h2>
        <div className="space-y-4">
          <div>
            <label className="text-sm text-slate-400">后端 API 地址</label>
            <input
              type="text"
              value={apiUrl}
              disabled
              className="w-full mt-1 bg-slate-700/50 rounded-lg px-4 py-2 text-slate-500"
            />
          </div>
          <div>
            <label className="text-sm text-slate-400 mb-2 block">API 组</label>
            <select
              value={selectedApiGroup}
              onChange={(e) => setSelectedApiGroup(e.target.value)}
              className="w-full bg-slate-700 rounded-lg px-4 py-3 focus:outline-none focus:ring-2 focus:ring-primary-500"
            >
              {config && Object.entries(config.api_groups).map(([key, group]) => (
                <option key={key} value={key}>
                  {group.name} - {group.description}
                </option>
              ))}
            </select>
            <div className="text-xs text-slate-500 mt-2">
              选择不同的API提供商（切换后自动重载，无需重启）
            </div>
          </div>
          <div>
            <label className="text-sm text-slate-400 mb-2 block">当前 API 组信息</label>
            <div className="bg-slate-700 rounded-lg px-4 py-3">
              <div className="font-medium">{currentGroup?.name}</div>
              <div className="text-sm text-slate-400 mt-1">{currentGroup?.description}</div>
              <div className="text-xs text-slate-500 mt-2">
                可用模型: {currentGroup?.models.length || 0} 个
              </div>
            </div>
          </div>
        </div>
      </div>

      {/* DeepSeek API 配置 */}
      <div className="bg-slate-800 rounded-lg p-6">
        <h2 className="text-xl font-semibold flex items-center mb-4">
          <KeyRound className="h-5 w-5 mr-2" />
          DeepSeekAPI配置
        </h2>
        {llmMessage && (
          <div className={`mb-4 border rounded-lg p-3 ${
            llmMessage.includes('失败') || llmMessage.includes('错误') || llmMessage.includes('不能为空')
              ? 'bg-red-500/10 border-red-500 text-red-400'
              : 'bg-green-500/10 border-green-500 text-green-400'
          }`}>
            {llmMessage}
          </div>
        )}
        <div className="space-y-4">
          <div>
            <label className="text-sm text-slate-400 mb-2 block">API组</label>
            <select
              value={llmApiGroup}
              onChange={(e) => setLlmApiGroup(e.target.value)}
              className="w-full bg-slate-700 rounded-lg px-4 py-3 focus:outline-none focus:ring-2 focus:ring-primary-500"
            >
              <option value="deepseek">deepseek</option>
              <option value="openai_compatible">openai_compatible</option>
            </select>
          </div>
          <div>
            <label className="text-sm text-slate-400 mb-2 block">BaseURL</label>
            <input
              type="text"
              value={llmBaseUrl}
              onChange={(e) => setLlmBaseUrl(e.target.value)}
              placeholder="https://api.deepseek.com/v1"
              className="w-full bg-slate-700 rounded-lg px-4 py-2 focus:outline-none focus:ring-2 focus:ring-primary-500"
            />
          </div>
          <div>
            <label className="text-sm text-slate-400 mb-2 block">APIKey</label>
            <input
              type="password"
              value={llmApiKey}
              onChange={(e) => setLlmApiKey(e.target.value)}
              placeholder="留空表示不修改现有APIKey"
              className="w-full bg-slate-700 rounded-lg px-4 py-2 focus:outline-none focus:ring-2 focus:ring-primary-500"
            />
            {llmConfig?.api_key_configured && (
              <div className="text-xs text-slate-500 mt-2">
                已配置：{llmConfig.api_key_masked}
              </div>
            )}
          </div>
          <div>
            <label className="text-sm text-slate-400 mb-2 block">Model</label>
            <input
              type="text"
              value={llmModel}
              onChange={(e) => setLlmModel(e.target.value)}
              placeholder="deepseek-chat"
              className="w-full bg-slate-700 rounded-lg px-4 py-2 focus:outline-none focus:ring-2 focus:ring-primary-500"
            />
          </div>
          <div className="flex flex-wrap gap-3">
            <button
              onClick={() => testLlmMutation.mutate()}
              disabled={testLlmMutation.isPending}
              className="flex items-center px-4 py-2 bg-slate-700 hover:bg-slate-600 rounded-lg transition-colors disabled:opacity-50"
            >
              {testLlmMutation.isPending && <Loader2 className="h-4 w-4 mr-2 animate-spin" />}
              测试连接
            </button>
            <button
              onClick={() => saveLlmMutation.mutate()}
              disabled={saveLlmMutation.isPending}
              className="flex items-center px-4 py-2 bg-primary-600 hover:bg-primary-500 rounded-lg transition-colors disabled:opacity-50"
            >
              {saveLlmMutation.isPending ? (
                <Loader2 className="h-4 w-4 mr-2 animate-spin" />
              ) : (
                <Save className="h-4 w-4 mr-2" />
              )}
              保存并重载
            </button>
          </div>
        </div>
      </div>

      {/* 现金设置 */}
      <div className="bg-slate-800 rounded-lg p-6">
        <h2 className="text-xl font-semibold flex items-center mb-4">
          <DollarSign className="h-5 w-5 mr-2" />
          现金余额
        </h2>
        <div className="space-y-4">
          <div>
            <label className="text-sm text-slate-400 mb-2 block">可用现金（元）</label>
            <input
              type="number"
              value={cash}
              onChange={(e) => setCash(parseFloat(e.target.value) || 0)}
              step="1000"
              className="w-full bg-slate-700 rounded-lg px-4 py-2 focus:outline-none focus:ring-2 focus:ring-primary-500"
            />
            <div className="text-xs text-slate-500 mt-2">
              设置账户中的可用现金余额，用于资产统计和建议计算
            </div>
          </div>
        </div>
      </div>

      {/* 模型设置 */}
      <div className="bg-slate-800 rounded-lg p-6">
        <h2 className="text-xl font-semibold flex items-center mb-4">
          <Cpu className="h-5 w-5 mr-2" />
          模型设置
        </h2>
        <div className="space-y-3">
          {availableModels.map((m) => (
            <label
              key={m.id}
              className={`flex items-center p-4 rounded-lg cursor-pointer transition-colors ${
                selectedModel === m.id ? 'bg-primary-600/20 border border-primary-500' : 'bg-slate-700 hover:bg-slate-600'
              }`}
            >
              <input
                type="radio"
                name="model"
                value={m.id}
                checked={selectedModel === m.id}
                onChange={(e) => setSelectedModel(e.target.value)}
                className="sr-only"
              />
              <div className="flex-1">
                <div className="font-medium flex items-center">
                  {m.name}
                  {m.supports_vision && (
                    <span className="ml-2 text-xs bg-blue-500/20 text-blue-400 px-2 py-0.5 rounded">
                      支持图片
                    </span>
                  )}
                </div>
                <div className="text-sm text-slate-400">{m.description}</div>
              </div>
              {selectedModel === m.id && (
                <div className="w-4 h-4 bg-primary-500 rounded-full" />
              )}
            </label>
          ))}
        </div>
      </div>

      {/* 保存按钮 */}
      <div className="flex justify-end">
        <button
          onClick={handleSave}
          disabled={switchModelMutation.isPending || updateCashMutation.isPending}
          className="flex items-center px-6 py-3 bg-primary-600 hover:bg-primary-500 rounded-lg transition-colors disabled:opacity-50"
        >
          {(switchModelMutation.isPending || updateCashMutation.isPending) ? (
            <Loader2 className="h-5 w-5 mr-2 animate-spin" />
          ) : (
            <Save className="h-5 w-5 mr-2" />
          )}
          保存设置
        </button>
      </div>
    </div>
  )
}
