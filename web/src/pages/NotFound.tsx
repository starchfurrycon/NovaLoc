/** Fallback route. */

import type { ReactNode } from 'react'
import { Link, useLocation } from 'react-router-dom'
import { Compass } from 'lucide-react'
import { EmptyState, Panel } from '../components/ui'

export default function NotFoundPage(): ReactNode {
  const { pathname } = useLocation()
  return (
    <Panel title="路由未匹配" icon={<Compass size={13} />} frame="nero">
      <EmptyState
        icon={<Compass size={20} />}
        title="404"
        description={`未找到与 ${pathname} 匹配的页面。`}
        action={
          <Link
            to="/"
            className="border border-cyan/50 px-3 py-1 text-[11px] tracking-[0.1em] text-cyan-soft uppercase hover:bg-cyan/12"
          >
            返回项目列表
          </Link>
        }
      />
    </Panel>
  )
}
