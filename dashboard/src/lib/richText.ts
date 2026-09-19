export interface TextRun {
  text: string
  bold: boolean
}

/**
 * Split the assistant's **bold** markers into runs. The result is plain data, so
 * the page renders it as text nodes and nothing a customer or the model wrote can
 * become markup.
 */
export function splitBold(input: string): TextRun[] {
  const runs: TextRun[] = []
  let last = 0
  for (const match of input.matchAll(/\*\*(.+?)\*\*/g)) {
    if (match.index > last) runs.push({ text: input.slice(last, match.index), bold: false })
    runs.push({ text: match[1], bold: true })
    last = match.index + match[0].length
  }
  if (last < input.length) runs.push({ text: input.slice(last), bold: false })
  return runs
}
