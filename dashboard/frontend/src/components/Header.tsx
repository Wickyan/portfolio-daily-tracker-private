import { RefreshCw, Bell } from 'lucide-react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { portfolioService } from '@/services'

export default function Header() {
  const queryClient = useQueryClient()
  const { data: portfolio } = useQuery({
    queryKey: ['portfolio'],
    queryFn: portfolioService.getPortfolio,
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

  return (
    <header className="flex h-16 items-center justify-between border-b border-slate-700 bg-slate-800 px-6">
      <div className="flex items-center space-x-8">
        <div>
          <div className="text-sm text-slate-400">总资产</div>
          <div className="text-lg font-semibold">
            {portfolio ? formatMoney(portfolio.total_assets) : '--'}
          </div>
        </div>
        <div>
          <div className="text-sm text-slate-400">持仓市值</div>
          <div className="text-lg font-semibold">
            {portfolio ? formatMoney(portfolio.total_market_value) : '--'}
          </div>
        </div>
        <div>
          <div className="text-sm text-slate-400">可用现金</div>
          <div className="text-lg font-semibold">
            {portfolio ? formatMoney(portfolio.cash) : '--'}
          </div>
        </div>
        <div>
          <div className="text-sm text-slate-400">总盈亏</div>
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
