param(
  [int]$MaxItems = 90
)

$ErrorActionPreference = 'Stop'
try {
  [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding $false
  $OutputEncoding = [Console]::OutputEncoding
} catch {}

try {
  Add-Type -AssemblyName UIAutomationClient
  Add-Type -AssemblyName UIAutomationTypes
  Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public static class NativeUiScan {
  [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
  [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr hWnd, out int processId);
}
'@

  function Convert-Rect($rect) {
    if ($null -eq $rect -or $rect.IsEmpty) { return $null }
    $x = [int][Math]::Round($rect.Left)
    $y = [int][Math]::Round($rect.Top)
    $w = [int][Math]::Round($rect.Width)
    $h = [int][Math]::Round($rect.Height)
    if ($w -lt 6 -or $h -lt 6) { return $null }
    [pscustomobject]@{
      x = $x
      y = $y
      width = $w
      height = $h
      right = $x + $w
      bottom = $y + $h
    }
  }

  function Test-Pattern($element, $pattern) {
    try {
      $null = $element.GetCurrentPattern($pattern)
      return $true
    } catch {
      return $false
    }
  }

  $hwnd = [NativeUiScan]::GetForegroundWindow()
  if ($hwnd -eq [IntPtr]::Zero) { throw "No foreground window." }

  $root = [System.Windows.Automation.AutomationElement]::FromHandle($hwnd)
  if ($null -eq $root) { throw "Unable to read foreground window by UI Automation." }

  $processId = 0
  [void][NativeUiScan]::GetWindowThreadProcessId($hwnd, [ref]$processId)
  $processName = ""
  try { $processName = (Get-Process -Id $processId -ErrorAction Stop).ProcessName } catch {}

  $rootCurrent = $root.Current
  $rootRect = Convert-Rect $rootCurrent.BoundingRectangle
  $rootArea = 0
  if ($null -ne $rootRect) { $rootArea = [double]($rootRect.width * $rootRect.height) }

  $wantedControls = @(
    "Button", "Edit", "Hyperlink", "MenuItem", "ListItem", "TabItem",
    "CheckBox", "RadioButton", "ComboBox", "TreeItem", "DataItem",
    "Slider", "Thumb", "ScrollBar", "SplitButton", "Spinner"
  )
  $blankNameAllowed = @("Edit", "Slider", "ScrollBar", "ComboBox", "Spinner")
  $containerControls = @("Pane", "Group", "Document")
  $items = New-Object System.Collections.ArrayList
  $queue = New-Object System.Collections.ArrayList
  $seen = @{}
  [void]$queue.Add($root)
  $index = 0
  $visited = 0
  $visitedLimit = 2500
  $walker = [System.Windows.Automation.TreeWalker]::ControlViewWalker

  while ($index -lt $queue.Count -and $visited -lt $visitedLimit -and $items.Count -lt $MaxItems) {
    $element = $queue[$index]
    $index += 1
    $visited += 1

    try {
      $child = $walker.GetFirstChild($element)
      while ($null -ne $child -and $queue.Count -lt $visitedLimit) {
        [void]$queue.Add($child)
        $child = $walker.GetNextSibling($child)
      }
    } catch {}

    if ($index -eq 1) { continue }

    try { $current = $element.Current } catch { continue }
    try { if ($current.IsOffscreen) { continue } } catch {}
    try { if (-not $current.IsEnabled) { continue } } catch {}

    $rect = Convert-Rect $current.BoundingRectangle
    if ($null -eq $rect) { continue }

    $control = ""
    try { $control = $current.ControlType.ProgrammaticName -replace '^ControlType\.', '' } catch {}
    if ([string]::IsNullOrWhiteSpace($control)) { continue }
    if ($control -eq "Text") { continue }

    $patterns = New-Object System.Collections.ArrayList
    if (Test-Pattern $element ([System.Windows.Automation.InvokePattern]::Pattern)) { [void]$patterns.Add("Invoke") }
    if (Test-Pattern $element ([System.Windows.Automation.ValuePattern]::Pattern)) { [void]$patterns.Add("Value") }
    if (Test-Pattern $element ([System.Windows.Automation.TogglePattern]::Pattern)) { [void]$patterns.Add("Toggle") }
    if (Test-Pattern $element ([System.Windows.Automation.SelectionItemPattern]::Pattern)) { [void]$patterns.Add("SelectionItem") }
    if (Test-Pattern $element ([System.Windows.Automation.ExpandCollapsePattern]::Pattern)) { [void]$patterns.Add("ExpandCollapse") }
    if (Test-Pattern $element ([System.Windows.Automation.RangeValuePattern]::Pattern)) { [void]$patterns.Add("RangeValue") }
    if (Test-Pattern $element ([System.Windows.Automation.ScrollItemPattern]::Pattern)) { [void]$patterns.Add("ScrollItem") }
    $actionPatternCount = @($patterns | Where-Object { $_ -ne "ScrollItem" }).Count

    $usefulControl = $wantedControls -contains $control
    if (-not $usefulControl -and $actionPatternCount -eq 0) { continue }

    $name = ""
    try { $name = (($current.Name -replace '\s+', ' ').Trim()) } catch {}
    $className = ""
    try { $className = $current.ClassName } catch {}
    if ([string]::IsNullOrWhiteSpace($name) -and $actionPatternCount -eq 0 -and -not ($blankNameAllowed -contains $control)) {
      continue
    }

    $area = [double]($rect.width * $rect.height)
    if ($rootArea -gt 0 -and $area -gt ($rootArea * 0.72) -and $actionPatternCount -eq 0) {
      continue
    }
    if ($className -match 'monaco-sash') { continue }
    if ($className -match 'statusbar-item' -or $className -match 'monaco-icon-label') { continue }
    if ($control -eq "ToolBar") { continue }
    if ($className -in @("WinCaptionButtonContainer", "menubar-menu-button")) { continue }
    if ($containerControls -contains $control) {
      if ([string]::IsNullOrWhiteSpace($name)) { continue }
      if ($rootArea -gt 0 -and $area -gt ($rootArea * 0.18)) { continue }
    }

    $key = "$($rect.x),$($rect.y),$($rect.width),$($rect.height),$control,$name"
    if ($seen.ContainsKey($key)) { continue }
    $seen[$key] = $true

    $label = $name
    if ([string]::IsNullOrWhiteSpace($label)) { $label = $control }
    if ($label.Length -gt 42) { $label = $label.Substring(0, 42) + "..." }

    [void]$items.Add([pscustomobject]@{
      id = $items.Count + 1
      label = $label
      name = $name
      control = $control
      x = $rect.x
      y = $rect.y
      width = $rect.width
      height = $rect.height
      centerX = [int]($rect.x + ($rect.width / 2))
      centerY = [int]($rect.y + ($rect.height / 2))
      patterns = @($patterns)
      automationId = $current.AutomationId
      className = $className
    })
  }

  if ($items.Count -gt 1) {
    $containerLike = @("Group", "Pane", "Document", "ToolBar", "List", "Tab")
    $kept = New-Object System.Collections.ArrayList
    for ($i = 0; $i -lt $items.Count; $i++) {
      $candidate = $items[$i]
      if ($containerLike -notcontains $candidate.control) {
        [void]$kept.Add($candidate)
        continue
      }
      $candidateArea = [Math]::Max(1, [double]($candidate.width * $candidate.height))
      $containsSpecificChild = $false
      for ($j = 0; $j -lt $items.Count; $j++) {
        if ($i -eq $j) { continue }
        $other = $items[$j]
        if ($containerLike -contains $other.control) { continue }
        $otherArea = [Math]::Max(1, [double]($other.width * $other.height))
        if ($candidateArea -le ($otherArea * 1.35)) { continue }
        if (
          $other.centerX -ge $candidate.x -and
          $other.centerX -le ($candidate.x + $candidate.width) -and
          $other.centerY -ge $candidate.y -and
          $other.centerY -le ($candidate.y + $candidate.height)
        ) {
          $containsSpecificChild = $true
          break
        }
      }
      if (-not $containsSpecificChild) { [void]$kept.Add($candidate) }
    }
    for ($i = 0; $i -lt $kept.Count; $i++) { $kept[$i].id = $i + 1 }
    $items = $kept
  }

  if ($null -ne $rootRect) {
    $rootItem = [pscustomobject]@{
      id = 1
      label = "Foreground window"
      name = $rootCurrent.Name
      control = "Window"
      x = $rootRect.x
      y = $rootRect.y
      width = $rootRect.width
      height = $rootRect.height
      centerX = [int]($rootRect.x + ($rootRect.width / 2))
      centerY = [int]($rootRect.y + ($rootRect.height / 2))
      patterns = @()
      automationId = $rootCurrent.AutomationId
      className = $rootCurrent.ClassName
    }
    $withRoot = New-Object System.Collections.ArrayList
    [void]$withRoot.Add($rootItem)
    foreach ($item in $items) { [void]$withRoot.Add($item) }
    $items = $withRoot
  }

  for ($i = 0; $i -lt $items.Count; $i++) {
    $items[$i].id = $i + 1
  }

  [pscustomobject]@{
    ok = $true
    hwnd = ("0x{0:X}" -f $hwnd.ToInt64())
    title = $rootCurrent.Name
    processId = $processId
    processName = $processName
    root = $rootRect
    count = $items.Count
    visited = $visited
    items = @($items)
  } | ConvertTo-Json -Depth 8 -Compress
} catch {
  [pscustomobject]@{
    ok = $false
    error = $_.Exception.Message
    items = @()
  } | ConvertTo-Json -Depth 4 -Compress
}
