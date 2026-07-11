import { RefreshCw, Bell } from 'lucide-react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { portfolioService } from '@/services'

export default function Header() {
  const queryClient = useQueryClient()
  const { data: portfolio } = useQuery({
    queryKey: ['portfolio'],
    queryFn: portfolioService.getLivePortfolio,
    refetchInterval: 60000,
    refetchOnWindowFocus: false,
  })

  const refreshMutation = useMutation({
    mutationFn: portfolioService.refresh,
    onSuccess: async () => {
      await queryClient.refetchQueries({ queryKey: ['portfolio'], type: 'active' })
    },
  })

  const formatMoney = (value: number) => {
    return new Intl.NumberFormat('zh-CN', {
      style: 'currency',
      currency: 'CNY',
    }).format(value)
  }

  const totalAssets = portfolio?.total_assets || 0
  const holdingPercent = totalAssets > 0 ? ((portfolio?.total_market_value || 0) / totalAssets) * 100 : 0
  const cashPercent = totalAssets > 0 ? ((portfolio?.cash || 0) / totalAssets) * 100 : 0

  return (
    <header className="flex h-16 items-center justify-between border-b border-slate-700 bg-slate-800 px-6">
      <div className="flex items-center space-x-8">
        <div>
          <div className="text-sm text-slate-400">总资产(CNY折算)</div>
          <div className="text-lg font-semibold">
            {portfolio ? formatMoney(portfolio.total_assets) : '--'}
          </div>
        </div>
        <div>
          <div className="text-sm text-slate-400">持仓市值(CNY)</div>
          <div className="flex items-baseline gap-2 text-lg font-semibold">
            <span>{portfolio ? formatMoney(portfolio.total_market_value) : '--'}</span>
            {portfolio && (
              <span className="text-sm font-normal text-slate-400">({holdingPercent.toFixed(2)}%)</span>
            )}
          </div>
        </div>
        <div>
          <div className="text-sm text-slate-400">账户现金(CNY)</div>
          <div className="flex items-baseline gap-2 text-lg font-semibold">
            <span>{portfolio ? formatMoney(portfolio.cash) : '--'}</span>
            {portfolio && (
              <span className="text-sm font-normal text-slate-400">({cashPercent.toFixed(2)}%)</span>
            )}
          </div>
        </div>
        <div>
          <div className="text-sm text-slate-400">总盈亏(CNY)</div>
          <div
            className={`text-lg font-semibold ${
              !portfolio || portfolio.total_profit >= 0 ? 'profit-positive' : 'profit-negative'
            }`}
          >
            {portfolio ? formatMoney(portfolio.total_profit) : '--'}
          </div>
        </div>
      </div>

      <div className="flex items-center space-x-4">
        <button
          onClick={() => refreshMutation.mutate()}
          disabled={refreshMutation.isPending}
          className="flex items-center px-3 py-2 text-slate-300 hover:text-white hover:bg-slate-700 rounded-lg transition-colors disabled:opacity-50"
          title="刷新行情"
        >
          <RefreshCw className={`h-5 w-5 ${refreshMutation.isPending ? 'animate-spin' : ''}`} />
        </button>
        <button
          className="flex items-center px-3 py-2 text-slate-300 hover:text-white hover:bg-slate-700 rounded-lg transition-colors"
          title="通知"
        >
          <Bell className="h-5 w-5" />
        </button>
      </div>
    </header>
  )
}
