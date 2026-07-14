import * as React from "react"
import { Command as CommandPrimitive } from "cmdk"
import { SearchIcon } from "lucide-react"

import { cn } from "@/lib/utils"

// When this list is nested inside a modal Dialog (e.g. category picker
// inside the transaction dialog), Radix's body scroll lock intercepts wheel
// and touch events at the document level and calls preventDefault on them
// because this list's portal isn't a DOM descendant of the Dialog content.
// That leaves native scrolling dead here. We detect the already-prevented
// event and drive scrollTop manually so wheel/touch keep working, while
// leaving normal (non-Dialog) usage untouched since defaultPrevented is
// false there and native scrolling still does the work.
function useManualScrollFallback() {
  const touchRef = React.useRef<{ y: number; scrollTop: number } | null>(null)

  const onWheel = React.useCallback((e: React.WheelEvent<HTMLDivElement>) => {
    if (e.defaultPrevented) {
      e.currentTarget.scrollTop += e.deltaY
    }
  }, [])

  const onTouchStart = React.useCallback((e: React.TouchEvent<HTMLDivElement>) => {
    const touch = e.touches[0]
    touchRef.current = { y: touch.clientY, scrollTop: e.currentTarget.scrollTop }
  }, [])

  const onTouchMove = React.useCallback((e: React.TouchEvent<HTMLDivElement>) => {
    if (!e.defaultPrevented || !touchRef.current) return
    const touch = e.touches[0]
    const deltaY = touchRef.current.y - touch.clientY
    e.currentTarget.scrollTop = touchRef.current.scrollTop + deltaY
  }, [])

  return { onWheel, onTouchStart, onTouchMove }
}

function Command({
  className,
  ...props
}: React.ComponentProps<typeof CommandPrimitive>) {
  return (
    <CommandPrimitive
      data-slot="command"
      className={cn(
        "bg-popover text-popover-foreground flex h-full w-full flex-col overflow-hidden rounded-md",
        className
      )}
      {...props}
    />
  )
}

function CommandInput({
  className,
  ...props
}: React.ComponentProps<typeof CommandPrimitive.Input>) {
  return (
    <div className="flex items-center border-b border-border px-3" data-slot="command-input-wrapper">
      <SearchIcon className="mr-2 h-4 w-4 shrink-0 opacity-50" />
      <CommandPrimitive.Input
        data-slot="command-input"
        className={cn(
          "placeholder:text-muted-foreground flex h-9 w-full rounded-md bg-transparent py-3 text-sm outline-hidden disabled:cursor-not-allowed disabled:opacity-50",
          className
        )}
        {...props}
      />
    </div>
  )
}

function CommandList({
  className,
  ...props
}: React.ComponentProps<typeof CommandPrimitive.List>) {
  const { onWheel, onTouchStart, onTouchMove } = useManualScrollFallback()
  return (
    <CommandPrimitive.List
      data-slot="command-list"
      className={cn("max-h-[300px] overflow-y-auto overflow-x-hidden overscroll-contain", className)}
      onWheel={onWheel}
      onTouchStart={onTouchStart}
      onTouchMove={onTouchMove}
      {...props}
    />
  )
}

function CommandEmpty({
  ...props
}: React.ComponentProps<typeof CommandPrimitive.Empty>) {
  return (
    <CommandPrimitive.Empty
      data-slot="command-empty"
      className="py-6 text-center text-sm text-muted-foreground"
      {...props}
    />
  )
}

function CommandGroup({
  className,
  ...props
}: React.ComponentProps<typeof CommandPrimitive.Group>) {
  return (
    <CommandPrimitive.Group
      data-slot="command-group"
      className={cn(
        "text-foreground overflow-hidden p-1 [&_[data-value]]:text-foreground",
        className
      )}
      {...props}
    />
  )
}

function CommandItem({
  className,
  ...props
}: React.ComponentProps<typeof CommandPrimitive.Item>) {
  return (
    <CommandPrimitive.Item
      data-slot="command-item"
      className={cn(
        "relative flex cursor-default select-none items-center gap-2 rounded-sm px-2 py-1.5 text-sm outline-hidden",
        "data-[selected=true]:bg-accent data-[selected=true]:text-accent-foreground data-[disabled=true]:pointer-events-none data-[disabled=true]:opacity-50",
        className
      )}
      {...props}
    />
  )
}

function CommandSeparator({
  className,
  ...props
}: React.ComponentProps<typeof CommandPrimitive.Separator>) {
  return (
    <CommandPrimitive.Separator
      data-slot="command-separator"
      className={cn("-mx-1 my-1 h-px bg-border", className)}
      {...props}
    />
  )
}

export {
  Command,
  CommandInput,
  CommandList,
  CommandEmpty,
  CommandGroup,
  CommandItem,
  CommandSeparator,
}
