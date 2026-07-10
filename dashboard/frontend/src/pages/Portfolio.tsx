import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { TrendingUp, TrendingDown, RefreshCw, Edit2, Trash2, X, Save, PlusCircle, RotateCcw } from 'lucide-react'
import { portfolioService } from '@/services'
import type { Position } from '@/types'

// 编辑持仓对话框组件
interface EditPositionDialogProps {
  position: Position
  onClose: () => void
  onSave: (identity: { account: string; code: string; currency: string }, data: { quantity: number; cost_price: number }) => void
}

function EditPositionDialog({ position, onClose, onSave }: EditPositionDialogProps) {
  const [quantity, setQuantity] = useState(position.quantity)
  const [costPrice, setCostPrice] = useState(position.cost_price)

  const handleSave = () => {
    onSave({ account: position.account, code: position.code, currency: position.currency }, { quantity, cost_price: costPrice })
    onClose()
  }

  return (
    <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50">
      <div className="bg-slate-800 rounded-lg p-6 w-full max-w-md">
        <div className="flex justify-between items-center mb-4">
          <h3 className="text-lg font-semibold">编辑持仓 - {position.name}</h3>
          <button onClick={onClose} className="text-slate-400 hover:text-white">
            <X className="h-5 w-5" />
          </button>
        </div>

        <div className="space-y-4">
          <div>
            <label className="block text-sm text-slate-400 mb-2">代码</label>
            <input
              type="text"
              value={position.code}
              disabled
              className="w-full bg-slate-700/50 rounded-lg px-4 py-2 text-slate-500"
            />
          </div>

          <div>
            <label className="block text-sm text-slate-400 mb-2">持仓数量</label>
            <input
              type="number"
              value={quantity}
              onChange={(e) => setQuantity(parseInt(e.target.value) || 0)}
              className="w-full bg-slate-700 rounded-lg px-4 py-2 focus:outline-none focus:ring-2 focus:ring-primary-500"
            />
          </div>

          <div>
            <label className="block text-sm text-slate-400 mb-2">成本价</label>
            <input
              type="number"
              step="0.01"
              value={costPrice}
              onChange={(e) => setCostPrice(parseFloat(e.target.value) || 0)}
              className="w-full bg-slate-700 rounded-lg px-4 py-2 focus:outline-none focus:ring-2 focus:ring-primary-500"
            />
          </div>

          <div className="flex space-x-3 pt-4">
            <button
              onClick={onClose}
              className="flex-1 px-4 py-2 bg-slate-700 hover:bg-slate-600 rounded-lg transition-colors"
            >
              取消
            </button>
            <button
              onClick={handleSave}
              className="flex-1 px-4 py-2 bg-primary-600 hover:bg-primary-500 rounded-lg transition-colors flex items-center justify-center"
            >
              <Save className="h-4 w-4 mr-2" />
              保存
            </button>
          </div>
        </div>
      </div>
    </div>
  )
}

function AddPositionDialog({ onClose, onSave }: {
  onClose: () => void
  onSave: (data: {
    account: string
    name: string
    code: string
    currency: string
    asset_type: string
    quantity: number
    cost_price: number
    fee?: number | null
    note?: string
  }) => void
}) {
  const [form, setForm] = useState({
    account: '',
    name: '',
    code: '',
    currency: 'CNY',
    asset_type: 'fund',
    quantity: '',
    cost_price: '',
    fee: '',
    note: '',
  })

  const setField = (field: string, value: string) => setForm(prev => ({ ...prev, [field]: value }))

  const handleSave = () => {
    onSave({
      account: form.account,
      name: form.name,
      code: form.code,
      currency: form.currency,
      asset_type: form.asset_type,
      quantity: Number(form.quantity),
      cost_price: Number(form.cost_price),
      fee: form.fee ? Number(form.fee) : null,
      note: form.note,
    })
    onClose()
  }

  return (
    <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50 p-4">
      <div className="bg-slate-800 rounded-lg p-6 w-full max-w-2xl">
        <div className="flex justify-between items-center mb-4">
          <h3 className="text-lg font-semibold">新增持仓</h3>
          <button onClick={onClose} className="text-slate-400 hover:text-white">
            <X className="h-5 w-5" />
          </button>
        </div>
        <div className="grid grid-cols-2 gap-4">
          {[
            ['account', '账户分组'],
            ['name', '标的名称'],
            ['code', '代码'],
            ['quantity', '数量'],
            ['cost_price', '成本价'],
            ['fee', '手续费'],
          ].map(([field, label]) => (
            <div key={field}>
              <label className="block text-sm text-slate-400 mb-2">{label}</label>
              <input
                type={['quantity', 'cost_price', 'fee'].includes(field) ? 'number' : 'text'}
                step="0.0001"
                value={(form as any)[field]}
                onChange={(e) => setField(field, e.target.value)}
                className="w-full bg-slate-700 rounded-lg px-4 py-2 focus:outline-none focus:ring-2 focus:ring-primary-500"
              />
            </div>
          ))}
          <div>
            <label className="block text-sm text-slate-400 mb-2">币种</label>
            <select
              value={form.currency}
              onChange={(e) => setField('currency', e.target.value)}
              className="w-full bg-slate-700 rounded-lg px-4 py-2 focus:outline-none focus:ring-2 focus:ring-primary-500"
            >
              <option value="CNY">CNY</option>
              <option value="USD">USD</option>
              <option value="HKD">HKD</option>
            </select>
          </div>
          <div>
            <label className="block text-sm text-slate-400 mb-2">类型</label>
            <select
              value={form.asset_type}
              onChange={(e) => setField('asset_type', e.target.value)}
              className="w-full bg-slate-700 rounded-lg px-4 py-2 focus:outline-none focus:ring-2 focus:ring-primary-500"
            >
              <option value="stock">stock</option>
              <option value="fund">fund</option>
              <option value="etf">etf</option>
              <option value="custom">custom</option>
              <option value="fund_or_custom">fund_or_custom</option>
            </select>
          </div>
          <div className="col-span-2">
            <label className="block text-sm text-slate-400 mb-2">备注</label>
            <input
              value={form.note}
              onChange={(e) => setField('note', e.target.value)}
              className="w-full bg-slate-700 rounded-lg px-4 py-2 focus:outline-none focus:ring-2 focus:ring-primary-500"
            />
          </div>
        </div>
        <div className="flex justify-end gap-3 pt-5">
          <button onClick={onClose} className="px-4 py-2 bg-slate-700 hover:bg-slate-600 rounded-lg transition-colors">取消</button>
          <button
            onClick={handleSave}
            disabled={!form.account || !form.name || !form.code || !form.quantity || !form.cost_price}
            className="flex items-center px-4 py-2 bg-primary-600 hover:bg-primary-500 rounded-lg transition-colors disabled:opacity-50"
          >
            <Save className="h-4 w-4 mr-2" />
            确认新增
          </button>
        </div>
      </div>
    </div>
  )
}

export default function Portfolio() {
  const queryClient = useQueryClient()
  const [editingPosition, setEditingPosition] = useState<Position | null>(null)
  const [showAddDialog, setShowAddDialog] = useState(false)
  const [autoRefresh, setAutoRefresh] = useState(true)

  const {
    data: portfolio,
    isLoading,
    refetch,
    isRefetching,
  } = useQuery({
    queryKey: ['portfolio', 'live'],
    queryFn: portfolioService.getLivePortfolio,
    refetchInterval: autoRefresh ? 60000 : false, // 每60秒自动刷新
    refetchOnWindowFocus: false,
  })

  const { data: operations = [], refetch: refetchOperations } = useQuery({
    queryKey: ['portfolio', 'operations'],
    queryFn: portfolioService.getOperations,
  })

  const addPositionMutation = useMutation({
    mutationFn: portfolioService.addPosition,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['portfolio'] })
      queryClient.invalidateQueries({ queryKey: ['portfolio', 'live'] })
      refetchOperations()
    }
  })

  const updatePositionMutation = useMutation({
    mutationFn: ({ identity, data }: { identity: { account: string; code: string; currency: string }; data: { quantity: number; cost_price: number } }) =>
      portfolioService.updatePosition(identity, data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['portfolio'] })
      queryClient.refetchQueries({ queryKey: ['portfolio'] })
      refetchOperations()
      setEditingPosition(null)
    }
  })

  const deletePositionMutation = useMutation({
    mutationFn: portfolioService.removePosition,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['portfolio'] })
      queryClient.refetchQueries({ queryKey: ['portfolio'] })
      refetchOperations()
    }
  })

  const rollbackMutation = useMutation({
    mutationFn: portfolioService.rollback,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['portfolio'] })
      queryClient.invalidateQueries({ queryKey: ['portfolio', 'live'] })
      refetchOperations()
    }
  })

  const handleDeletePosition = (position: Position) => {
    if (confirm(`确定要删除${position.account}账户的持仓${position.name}(${position.code}, ${position.currency})吗？`)) {
      deletePositionMutation.mutate({
        account: position.account,
        code: position.code,
        currency: position.currency,
      })
    }
  }

  const formatNumber = (num: number) => {
    return new Intl.NumberFormat('zh-CN', {
      minimumFractionDigits: 2,
      maximumFractionDigits: 2
    }).format(num)
  }

  const formatPercent = (num: number) => {
    return new Intl.NumberFormat('zh-CN', {
      minimumFractionDigits: 2,
      maximumFractionDigits: 2,
      signDisplay: 'exceptZero'
    }).format(num)
  }

  if (isLoading) {
    return (
      <div className="flex items-center justify-center h-full">
        <RefreshCw className="h-8 w-8 animate-spin text-primary-500" />
      </div>
    )
  }

  const positions = portfolio?.positions || []
  const totalAssets = portfolio?.total_assets || 0
  const totalMarketValue = portfolio?.total_market_value || 0
  const cash = portfolio?.cash || 0
  const totalProfit = portfolio?.total_profit || 0
  const profitPercent = totalMarketValue > 0 ? (totalProfit / (totalMarketValue - totalProfit)) * 100 : 0

  return (
    <div className="p-6 space-y-6">
      {/* 资产概览 */}
      <div className="grid grid-cols-4 gap-4">
        <div className="bg-slate-800 rounded-lg p-6">
          <div className="text-sm text-slate-400 mb-2">总资产</div>
          <div className="text-2xl font-bold">¥{formatNumber(totalAssets)}</div>
        </div>
        <div className="bg-slate-800 rounded-lg p-6">
          <div className="text-sm text-slate-400 mb-2">持仓市值</div>
          <div className="text-2xl font-bold">¥{formatNumber(totalMarketValue)}</div>
        </div>
        <div className="bg-slate-800 rounded-lg p-6">
          <div className="text-sm text-slate-400 mb-2">可用现金</div>
          <div className="text-2xl font-bold text-green-400">¥{formatNumber(cash)}</div>
        </div>
        <div className="bg-slate-800 rounded-lg p-6">
          <div className="text-sm text-slate-400 mb-2">总盈亏</div>
          <div className={`text-2xl font-bold ${totalProfit >= 0 ? 'text-green-400' : 'text-red-400'}`}>
            {totalProfit >= 0 ? '+' : ''}¥{formatNumber(totalProfit)}
            <span className="text-sm ml-2">({formatPercent(profitPercent)}%)</span>
          </div>
        </div>
      </div>

      {/* 持仓明细 */}
      <div className="bg-slate-800 rounded-lg">
        <div className="p-6 border-b border-slate-700 flex justify-between items-center">
          <div className="flex items-center space-x-4">
            <h2 className="text-xl font-semibold">持仓明细</h2>
            <label className="flex items-center text-sm text-slate-400 cursor-pointer">
              <input
                type="checkbox"
                checked={autoRefresh}
                onChange={(e) => setAutoRefresh(e.target.checked)}
                className="mr-2 rounded"
              />
              自动刷新(60s)
            </label>
          </div>
          <div className="flex gap-3">
            <button
              onClick={() => setShowAddDialog(true)}
              className="flex items-center px-4 py-2 bg-slate-700 hover:bg-slate-600 rounded-lg transition-colors"
            >
              <PlusCircle className="h-4 w-4 mr-2" />
              新增持仓
            </button>
            <button
              onClick={() => refetch()}
              disabled={isRefetching}
              className="flex items-center px-4 py-2 bg-primary-600 hover:bg-primary-500 rounded-lg transition-colors disabled:opacity-50"
            >
              <RefreshCw className={`h-4 w-4 mr-2 ${isRefetching ? 'animate-spin' : ''}`} />
              刷新行情
            </button>
          </div>
        </div>

        {positions.length === 0 ? (
          <div className="p-12 text-center text-slate-400">
            暂无持仓，点击对话框上传截图或添加持仓
          </div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full">
              <thead>
                <tr className="border-b border-slate-700">
                  <th className="px-6 py-4 text-left text-sm font-medium text-slate-400">账户</th>
                  <th className="px-6 py-4 text-left text-sm font-medium text-slate-400">代码</th>
                  <th className="px-6 py-4 text-left text-sm font-medium text-slate-400">名称</th>
                  <th className="px-6 py-4 text-left text-sm font-medium text-slate-400">币种/类型</th>
                  <th className="px-6 py-4 text-right text-sm font-medium text-slate-400">盈亏</th>
                  <th className="px-6 py-4 text-right text-sm font-medium text-slate-400">持仓/可用</th>
                  <th className="px-6 py-4 text-right text-sm font-medium text-slate-400">成本/现价</th>
                  <th className="px-6 py-4 text-right text-sm font-medium text-slate-400">市值</th>
                  <th className="px-6 py-4 text-right text-sm font-medium text-slate-400">盈亏比例</th>
                  <th className="px-6 py-4 text-center text-sm font-medium text-slate-400">操作</th>
                </tr>
              </thead>
              <tbody>
                {positions.map((position) => {
                  const isProfit = position.profit >= 0
                  const profitClass = isProfit ? 'text-green-400' : 'text-red-400'

                  return (
                    <tr key={`${position.account}-${position.code}-${position.currency}`} className="border-b border-slate-700/50 hover:bg-slate-700/30">
                      <td className="px-6 py-4">{position.account || '-'}</td>
                      <td className="px-6 py-4 font-mono text-primary-400">{position.code}</td>
                      <td className="px-6 py-4">{position.name}</td>
                      <td className="px-6 py-4">
                        <div className="text-sm">{position.currency}</div>
                        <div className="text-xs text-slate-500">{position.asset_type}</div>
                      </td>
                      <td className={`px-6 py-4 text-right font-medium ${profitClass}`}>
                        <div className="flex items-center justify-end">
                          {isProfit ? <TrendingUp className="h-4 w-4 mr-1" /> : <TrendingDown className="h-4 w-4 mr-1" />}
                          {isProfit ? '+' : ''}¥{formatNumber(position.profit)}
                        </div>
                      </td>
                      <td className="px-6 py-4 text-right">
                        <div className="text-sm">{position.quantity}</div>
                        <div className="text-xs text-slate-500">{position.available_qty}</div>
                      </td>
                      <td className="px-6 py-4 text-right">
                        <div className="text-sm text-slate-400">¥{formatNumber(position.cost_price)}</div>
                        <div className={`text-sm font-medium ${profitClass}`}>¥{formatNumber(position.current_price)}</div>
                      </td>
                      <td className="px-6 py-4 text-right font-medium">
                        ¥{formatNumber(position.market_value)}
                      </td>
                      <td className={`px-6 py-4 text-right font-bold ${profitClass}`}>
                        {formatPercent(position.profit_pct)}%
                      </td>
                      <td className="px-6 py-4">
                        <div className="flex items-center justify-center space-x-2">
                          <button
                            onClick={() => setEditingPosition(position)}
                            className="p-1.5 text-slate-400 hover:text-primary-400 hover:bg-slate-700 rounded transition-colors"
                            title="编辑持仓"
                          >
                            <Edit2 className="h-4 w-4" />
                          </button>
                          <button
                            onClick={() => handleDeletePosition(position)}
                            className="p-1.5 text-slate-400 hover:text-red-400 hover:bg-slate-700 rounded transition-colors"
                            title="删除持仓"
                          >
                            <Trash2 className="h-4 w-4" />
                          </button>
                        </div>
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        )}
      </div>

      <div className="bg-slate-800 rounded-lg p-6">
        <h2 className="text-xl font-semibold mb-4">操作日志</h2>
        {operations.length === 0 ? (
          <div className="text-slate-400">暂无操作记录</div>
        ) : (
          <div className="space-y-2">
            {operations.slice(0, 8).map(operation => (
              <div key={operation.operation_id} className="flex items-center justify-between rounded bg-slate-700/50 px-4 py-3">
                <div>
                  <div className="font-medium">{operation.summary}</div>
                  <div className="text-xs text-slate-400">{operation.type} · {operation.created_at}</div>
                </div>
                <button
                  onClick={() => rollbackMutation.mutate(operation.operation_id)}
                  disabled={!operation.can_rollback || rollbackMutation.isPending}
                  className="flex items-center rounded bg-slate-700 px-3 py-1.5 text-sm hover:bg-slate-600 disabled:opacity-50"
                >
                  <RotateCcw className="h-4 w-4 mr-2" />
                  回滚
                </button>
              </div>
            ))}
          </div>
        )}
      </div>

      {/* 编辑对话框 */}
      {editingPosition && (
        <EditPositionDialog
          position={editingPosition}
          onClose={() => setEditingPosition(null)}
          onSave={(identity, data) => updatePositionMutation.mutate({ identity, data })}
        />
      )}
      {showAddDialog && (
        <AddPositionDialog
          onClose={() => setShowAddDialog(false)}
          onSave={(data) => addPositionMutation.mutate(data)}
        />
      )}
    </div>
  )
}
