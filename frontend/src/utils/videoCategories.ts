import type { VideoCategory } from '../services/api'

/**
 * Same list as GET /api/v1/video-categories. Used only when that payload
 * does not include an array, so the import screen can still offer a category.
 */
export const DEFAULT_VIDEO_CATEGORIES: VideoCategory[] = [
  {
    value: 'default',
    name: '默认',
    description: 'General video content processing',
    icon: '🎬',
    color: '#4facfe',
  },
  {
    value: 'knowledge',
    name: '知识科普',
    description: 'Knowledge content such as science, technology, history, and culture',
    icon: '📚',
    color: '#52c41a',
  },
  {
    value: 'entertainment',
    name: '娱乐',
    description: 'Entertainment content such as games, music, and movies',
    icon: '🎮',
    color: '#722ed1',
  },
  {
    value: 'business',
    name: '商业',
    description: 'Business content such as business, entrepreneurship, and investing',
    icon: '💼',
    color: '#fa8c16',
  },
  {
    value: 'experience',
    name: '经验分享',
    description: 'Experience content such as personal stories and life insights',
    icon: '🌟',
    color: '#eb2f96',
  },
  {
    value: 'opinion',
    name: '观点评论',
    description: 'Commentary content such as current-affairs commentary and opinion analysis',
    icon: '💭',
    color: '#13c2c2',
  },
  {
    value: 'speech',
    name: '演讲',
    description: 'Speech content such as public speeches and lectures',
    icon: '🎤',
    color: '#f5222d',
  },
]

export function categoryOptions(categories: unknown): VideoCategory[] {
  return Array.isArray(categories) ? categories as VideoCategory[] : DEFAULT_VIDEO_CATEGORIES
}

/**
 * Normalize GET /video-categories.
 * An array (including empty) is kept. null, a missing field, or a non-array
 * falls back to the built-in list so callers never store a value without .map.
 */
export function applyCategoryResponse(response: unknown): {
  categories: VideoCategory[]
  selectedCategory?: string
} {
  const payload = response && typeof response === 'object'
    ? response as { categories?: unknown; default_category?: unknown }
    : {}
  const categories = categoryOptions(payload.categories)
  const preferred = payload.default_category
  if (typeof preferred === 'string' && preferred.length > 0) {
    return { categories, selectedCategory: preferred }
  }
  if (categories.length > 0) {
    return { categories, selectedCategory: categories[0].value }
  }
  return { categories }
}
