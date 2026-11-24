import { useEffect, useRef, useState } from 'react'
import { animate } from 'motion'

const calm = () => window.matchMedia('(prefers-reduced-motion: reduce)').matches

/** Counts a figure up once, then follows changes. Under a second, and skipped when the visitor asks for less motion. */
export function CountUp({ value }: { value: number }) {
  const [shown, setShown] = useState(0)
  const [still] = useState(calm)
  const current = useRef(0)

  useEffect(() => {
    if (still) return
    const controls = animate(current.current, value, {
      duration: 0.7,
      ease: [0.23, 1, 0.32, 1],
      onUpdate: (latest) => {
        current.current = latest
        setShown(Math.round(latest))
      },
      onComplete: () => {
        current.current = value
        setShown(value)
      },
    })
    return () => controls.stop()
  }, [value, still])

  return <>{still ? value : shown}</>
}
