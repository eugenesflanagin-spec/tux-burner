# Tux Burner - Glossary

Every technical word in the guide, in plain English.

Tux Burner is Copyright (C) 2026 by Eugene Flanagin. Licensed under GPL-3.0-or-later. It is a fork of PenguinBurner by jpietek.

| Word | Plain meaning |
| --- | --- |
| **Adaptive runtime** | An upstream feature: while you play, the daemon switches between your Efficiency, Balanced and Performance profiles depending on the frame rate. |
| **amdgpu** | The Linux kernel driver for AMD Radeon cards. Tux Burner talks to it through files under `/sys/class/drm/`. |
| **App floor** | The deepest voltage offset the window and `tburn-cli` allow. Default -150 mV. |
| **Auto-UV** | "Automatic undervolt": the search for a good voltage setting. On AMD it is the *ladder*. |
| **Balanced** | A tier: the power cap that was set when the scan started, with the found voltage. |
| **Boot profile** | A profile the root service applies when the PC starts. Off by default. |
| **Clock** | How fast a part of the GPU runs, in MHz. SCLK is the core clock, MCLK the memory clock. |
| **Daemon** | A program that runs in the background. Here: `tux-burnerd`, the root service that does every GPU write. |
| **Daemon floor** | The root service's own limit on the voltage offset. It refuses deeper writes whatever the window says. |
| **Dry run** | Show what would be done and do nothing. |
| **Edge / junction** | Two GPU temperatures. Junction (the hot spot) is the hotter one; the fan curve follows it. |
| **Efficiency** | A tier: the found voltage with the card's default power cap. |
| **Fan curve** | A list of "at this temperature, run the fan at this percent" points. The AMD firmware accepts up to five. |
| **Floor** | See *App floor* and *Daemon floor*. |
| **Fork** | A copy of a project that goes its own way. Tux Burner is a fork of PenguinBurner. |
| **GPL-3.0-or-later** | The free-software licence of Tux Burner and of PenguinBurner. |
| **hwmon** | The kernel's folder of sensor readings (temperature, power, fan, voltage). |
| **Instability** | The card makes wrong results, glitches, resets the driver or freezes the PC. |
| **LACT** | "Linux AMDGPU Control Application", a popular tool for tuning AMD cards. Its service is `lactd`. |
| **Ladder** | The AMD Auto-UV search: 0 mV, -10, -20 ... one *rung* at a time, with a stress test on each. |
| **Margin** | Extra safety added to the deepest passing rung (default 20 mV, so the saved value is shallower). |
| **MCLK max** | The ceiling of the memory clock, in MHz. |
| **mV** | Millivolt, a thousandth of a volt. |
| **OpenCL** | A standard for running calculations on the GPU. Tux Burner's stress test uses it on AMD. |
| **OverDrive** | AMD's name for tuning controls in the driver. Tux Burner reads and writes them through `pp_od_clk_voltage`. |
| **Overlay** | The in-game frame-rate display (an upstream feature). |
| **Performance** | A tier: your core offset and the highest power cap. |
| **PMFW** | The firmware on the GPU that runs the fan curve. |
| **Power cap** | The most power, in watts, the card may use. |
| **Profile** | A saved plan (voltage, clocks, power, tier). |
| **Rung** | One step of the ladder. |
| **SCLK offset** | How many MHz to add to (or take from) the core clock. |
| **Stock** | The driver's default settings. |
| **Stress test** | A heavy GPU workload that checks its own answers, used to see whether a setting is stable. |
| **sysfs** | The kernel's folder of files under `/sys` that expose hardware. |
| **Tier** | One of Efficiency, Balanced, Performance. |
| **Undervolt** | Lower the voltage. Less power and heat, if the card stays stable. |
| **VDDGFX offset** | The AMD voltage knob: a global offset in mV added to the card's voltage table. Negative is an undervolt. |
| **Zero RPM** | The mode in which the fans stop at low temperature. |
