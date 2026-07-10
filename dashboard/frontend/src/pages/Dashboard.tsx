import ChatPanel from '@/components/ChatPanel'
import PortfolioPanel from '@/components/PortfolioPanel'
import SuggestionsPanel from '@/components/SuggestionsPanel'

export default function Dashboard() {
  return (
    <div className="relative grid h-full min-h-0 grid-cols-1 gap-6 overflow-hidden lg:grid-cols-12">
      {/* 左侧：对话区域 */}
      <div className="col-span-1 h-full min-h-0 lg:col-span-7">
        <ChatPanel />
      </div>

      {/* 右侧：持仓和建议 */}
      <div className="col-span-1 flex h-full min-h-0 flex-col gap-6 overflow-y-auto pb-4 custom-scrollbar lg:col-span-5">
        <PortfolioPanel />
        <SuggestionsPanel />
      </div>
    </div>
  )
}
