# plan.md — TF 卡批量写入：真实缺陷核验与严重问题总调度计划

> 状态：**契约/派工计划已形成，尚未派 subagent、未修改业务代码、未运行物理盘写入测试**。
> 核对日期：2026-11-23。核对范围：当前磁盘上的 SPEC.md、tf_flasher.py、tf_flasher.bat、flasher_build/_test_write_e2e.py、flasher_build/_test_write_multi.py、AGENTS.md；参考 E:/WebGemini/总调度模式 .md。
> 本计划只处理已核实的严重风险（P0/P1）及开展安全测试的前置条件。较低严重度单独进入待办，不混入本轮承诺。
> 原则：**数据不被误擦 > 明确失败 > 正确审计 > 速度与便利性**。所有写盘前置保护必须先于打开 PhysicalDrive 写句柄。

## 一、判断尺度与当前核验结论

- **证据等级**：A＝代码路径检查 + 安全的真实运行复现；B＝现行代码路径静态确证，触发需特定条件/设备；C＝仅理论防御，不把它写成当前已发生故障。
- **P0**：可能误删源盘/非目标盘数据，或未能独占挂载卷却破坏文件系统，需先修后继续现场批量写卡。
- **P1**：可能把无效内容当合法镜像写入、造成错误失败、或把未执行的校验报告成已完成；应在下一次交付前修。
- **P2**：影响可靠性、可用性或操作留痕但不直接证明会误写盘；排入后续包。
- **P3**：纯体验/防御性改进，不占用本轮高危修复名额。

| SPEC 条目 | 核验结果 | 优先级 | 代码证据与必要纠偏 |
|---|---|---|---|
| #1 尾部补零计入对账 | **真实、条件触发**（B） | P1 | flash_one 546–554 将 pad 加入 sha 和 n；563–567 与原始镜像大小/哈希对账，非扇区对齐时先在对账处失败，**实际上不会走到读回校验**；不能说当前读回阶段也已执行并失败。 |
| #2 无镜像内容校验 | **真实**（A） | **P0** | analyze_image 360–391 对非 .gz 按任意原始文件处理；已只读运行 python tf_flasher.py --image SPEC.md，返回 6183 B 和 SHA-256，退出成功。GUI start 892–898 在完整预扫描完成前启动写盘线程。**原清单“zip/文本/零字节都会写满卡”不准确**：写入量是源文件长度；零字节可能直接报成功却没写盘；非对齐文件还会因 #1 对账失败。 |
| #3 未排除镜像/工具所在盘 | **真实、条件触发**（B） | **P0** | selectable 281–289 无路径所属磁盘判断；GUI start 837–852、CLI cli_flash 1126–1142 也没有源盘/程序盘禁写；镜像位于符合容量/可移除条件的设备上时存在自擦风险。 |
| #4 卷锁定/弹出失败仍继续 | **真实、条件触发**（B） | **P0** | lock_volumes 292–313 仅 warnings；flash_one 504–516 继续打开 PhysicalDrive。锁失败而弹出成功同样继续；_volume_map 219–237 仅按盘符列卷，无盘符挂载点不覆盖。 |
| #5 frozen exe 提权重启 | **真实但正常打包路径被掩盖**（B） | P2 | relaunch_elevated 323–326 固定使用 -I + __file__；onefile 的 __file__ 不是 exe 路径，且 frozen exe 不应接收 Python -I；当前 --uac-admin manifest 通常绕开分支。尚未实测无 manifest 的 frozen 包。 |
| #6 台账异常未捕获 | **真实、条件触发**（B） | P2 | GUI _finish 996 与 CLI cli_flash 1200 直接调用 write_run_log（608–624），写目录受限会中断正常收尾；需以后续包修。 |
| #7 校验选项可中途修改 | **真实、条件触发**（B） | P1 | App.__init__ 691–694 创建复选框未留控件引用；_set_running 905–914 未禁用；start 897 在启动 worker 时读取值；_finish 996–1003 则读取结束时值，可能错误宣称“校验通过”。 |
| #8 pending 无兜底 | **当前未找到正常可触发路径**（C） | P3 | flash_one 503–605 主体捕获普通异常并发 final；try 外有字段访问但由枚举返回保证。未来改动可补外层 guard，**不应宣称已复现 UI 永久卡死**。 |
| #9 BAT 参数/CLI 进度 | **真实**（B） | P3 | tf_flasher.bat 最后一行未透传 %*；CLI 1167–1184 沿用写入阶段的进度基线，校验阶段可能始终不打印新进度。 |

**额外发现（不在 SPEC，作为所有写盘测试的阻断项）**：
flasher_build/_test_write_e2e.py 的 main() 接收显式 PhysicalDriveIndex 后，仅查“盘号存在”，**没有验证它是专用 VHD**；_test_write_multi.py 自动选所有 bus=15、≤128MiB 的磁盘，也不能证明就是本项目创建的 VHD。误用测试命令同样可能破坏其他盘。因此增加「S0 测试护栏」作为实施的**第一步**。

**未做的验证**：未对任何真实 TF 卡或 VHD 执行写入；未用未锁定卷、无盘符卷、只读目录或打包 exe 实测。代码确证不等于设备现场已复现。当前只完成语法检查（python -m py_compile 无输出、通过）和非写盘的非法格式复现。

## 二、总调度契约与停工条件

### 2.1 必须先明确的业务契约

1. **镜像类型**：默认只支持完整磁盘镜像（裸 .img 或 gzip 封装），接受结构有效的 MBR/GPT；不得仅凭 0x55AA 单一字节判断合法，也不得未经确认就把“所有无 MBR 的合法分区镜像”一概定义为错误。**是否支持分区镜像/无分区表镜像需用户裁决**，未裁决先采用“仅完整磁盘镜像”的安全默认值。
2. **镜像门禁**：非法扩展名/魔数不符、空文件、不可解析的分区布局、gzip 截断/校验失败、容量超过目标盘，均应在任何 WriteFile/FSCTL_DISMOUNT_VOLUME 前拒绝；首次完整预扫描可增加等待，但必须有进度、可取消。最小镜像体积阈值及 GPT 保护 MBR 兼容细节在实现前用测试例固定，不拍脑袋设置“统一 1 GiB”之类限制。
3. **源盘保护**：镜像所在卷对应的所有物理盘、运行 exe/脚本所在卷对应的所有物理盘，不得成为写盘目标；映射失败时默认拒绝写入（fail closed）。考虑盘符、目录挂载点、多 extent 卷和 UNC/网络路径，网络来源不得被误认成本地目标。
4. **卷保护**：只有完全列举目标盘关联卷，并对全部已挂载卷取得独占锁且成功弹出后才能写；锁失败、弹出失败或枚举结果不可靠则拒写该盘；允许真正无挂载卷的空白卡，但须可验证“枚举完成”。
5. **哈希/对账**：逻辑镜像字节数与 SHA-256 始终排除尾部填充；扇区补零只是介质 I/O 适配；读回仅比对镜像原始长度。运行审计按实际启动时冻结的 verify 选项，**不可按完成时 UI 当前状态回填**。
6. **失败语义**：预检失败不应造成写入；任一盘拒写不能影响其他盘的锁释放；任何已写入但取消/失败的盘必须显著标注“内容可能不完整”。禁止为达成测试通过而自动格式化、修复或清空真实磁盘。

### 2.2 Ponytail/验收路由

- 本轮为跨物理设备/卷枚举/GUI/CLI 状态链的高影响改动，采用 **Acceptance-First**；不引入额外第三方运行依赖、数据库或不必要的 DDD 分层。
- 权威边界：**安全的专用 VHD 单盘 + 多路写入与读回**，辅以纯单元/模拟 Win32 测试核验拒写路径；不得用“模拟全绿”代替真实 Windows VHD 终验。
- 顺序：先捕获修改前 RED（已有静态证据可复用）→ 最小测试 → 修根因 → 聚焦测试 → 受限 VHD 验收。严禁对真卡实施“故意写错文件”等破坏性复现。
- 当前工作区已经有未提交资产：tf_flasher.py、AGENTS.md、.gitignore 被修改，SPEC.md、assets/ 等未跟踪。**这些是前置基线，不属于任何子代理可领取的变更。**总调度派工前须记录 git status/diff；未获得所有权归属前禁止直接对包含原有脏内容的文件执行整文件 git add/commit。优先经授权使用独立 worktree 和纯净基线；若必须在当前工作区，严格逐 hunk 暂存并核对 staged diff。不得擅自提交用户已有改动。
- 禁止将 .img、.img.gz、VHD、flash_cache/、flash_logs/、exe、.venv、.codegraph 等测试/索引产物加入 Git。

### 2.3 派工文件所有权与交接纪律

| 角色 | 所有权范围 | 禁碰范围 |
|---|---|---|
| **总调度（唯一裁决人）** | plan.md、SPEC.md 的契约裁决/回勾；逐包审查、统一提交 | 不代替 subagent 编写业务实现、测试或跑设备实验 |
| **SA-IO（写盘核心代理）** | tf_flasher.py（仅当前放行步骤的相关符号），**任何时刻仅此一个代理** | 契约文件、测试文件、BAT、用户已有 icon 修改 |
| **SA-TEST（安全测试代理）** | flasher_build/vhd_guard.py、flasher_build/_test_write_e2e.py、flasher_build/_test_write_multi.py、tests/test_volume_safety.py、tests/test_source_guard.py、tests/test_image_preflight.py、tests/test_tail_accounting.py、tests/test_verify_snapshot.py（只领取当前步骤列出的文件） | tf_flasher.py、契约文件、其他本地 VHD、未登记的真实设备 |
| **SA-REVIEW（独立复验代理）** | 只读代码、测试输出和证据报告；**只准在证据指定路径创建报告** | 不修改源码或契约，不提交、不重启服务，不写真实设备 |

- 派发方式：按总调度模式**异步派包**，任务完成通知回来再由总调度判定；本文件只是派工合同，**尚未实际派发**。
- **共享源码串行**：S2–S6 都碰 tf_flasher.py，SA-IO 在当前步骤验收并释放文件所有权前，下一步不得开始编辑；测试代理仅在独立测试文件上并行准备，跨步骤同一测试文件也必须等待移交。
- 每包纪律块：仅改清单内文件；对所有其他文件只读；禁止 git commit/push、禁止 git add -A、禁止改或覆盖用户在途文件、禁止重启无关服务；UTF-8/LF；失败只写报告不自行扩大任务范围；报告需含 git diff --name-only、相关测试命令/结果、证据路径、风险与未验证项。
- 参考 E:/WebGemini/总调度模式 .md **只读禁改**。发生契约冲突，子代理停止并回报，总调度在 SPEC.md/plan.md 裁决后重新派工。

## 三、详细顺序任务包

### S1 / P0：先封死 VHD 测试脚本误写真实盘的入口

**步骤目标**：任何 E2E 写盘命令只能访问**经唯一身份核验的本项目专用 VHD**；传入任意其他 PhysicalDriveN 必须在打开写句柄前拒绝。

**可能改动的文件清单**：
- flasher_build/vhd_guard.py（新增：只读查询 VHD backing path、挂载关联与专用白名单）。
- flasher_build/_test_write_e2e.py（显式索引及自动选择均调用 guard）。
- flasher_build/_test_write_multi.py（多路目标逐一调用同一 guard）。
- tests/test_vhd_guard.py（新增：纯模拟拒绝路径）。

**参考声明**：参考 AGENTS.md §7 的 VHD 测试台，以及 flasher_build/mk_vhd_script.py、mk_vhd_more.py 中创建的专用 VHD 文件名/路径；采纳“由创建脚本登记的 VHD”身份来源，**放弃**“bus=15 且容量小即可无条件认为安全”的旧判断。

**复用声明**：复用现有 enumerate_disks() 获取物理盘号，复用 Windows DiskImage/磁盘关联查询只读获取 VHD 背书（例如 Get-DiskImage -ImagePath 关联 Get-Disk）；新建 guard 是为了单盘和多盘脚本**共用一套拒写条件**，不允许各写一套白名单。

**建议分配的 subagent**：SA-TEST 独占本步四文件；SA-REVIEW 只读复核测试盘定位。总调度处理身份契约裁决；SA-IO 不进入本步文件。

**验收标准**：
- 给普通 U 盘/系统盘/未知 PhysicalDriveN 或未登记小 VHD 指定索引：脚本**在调用 flash_one 前**拒绝且退出非 0。
- 断开或替换测试 VHD 后原索引不再可信；重新定位只接受当前实际挂载的登记 VHD。
- 双/三路测试仅遍历**登记 VHD 列表**，其他 bus=15 磁盘绝不进入候选；无符合磁盘时拒绝，不尝试自动修复或创建盘。
- 纯模拟测试通过；提交前记录拒绝行为证据。**本步骤允许验证 guard 的拒绝分支，不允许在 guard 验收前跑现有真实写盘 E2E。**

### S2 / P0：卷拓扑完整枚举 + 锁定/弹出失败即拒写（SPEC #4）

**步骤目标**：写盘前准确找出目标盘相关的全部卷并独占；任一锁/弹出失败或卷拓扑不确定时，目标盘零数据写入。

**可能改动的文件清单**：
- tf_flasher.py（_volume_map、lock_volumes、flash_one，以及所需 Win32 声明；不碰 UI 布局和其他功能）。
- tests/test_volume_safety.py（新增：盘符/目录挂载、多 extent、失败清理的聚焦测试）。

**参考声明**：参考 tf_flasher.py:_local_volume_letters、_volume_map 以及 lock_volumes；复用其 Win32 ctypes 访问方式与 held 句柄释放流程；**放弃**“只遍历盘符”和“失败只警告继续”两项行为。参考 Windows 卷管理 APIs：FindFirstVolume/FindNextVolume、IOCTL_VOLUME_GET_VOLUME_DISK_EXTENTS 与 FSCTL_LOCK_VOLUME/FSCTL_DISMOUNT_VOLUME；实现前核实 Windows 版本和错误码语义。

**复用声明**：沿用 _open/_ioctl、_RawDiskFile 与 flash_one 的 finally 释放入口；单一“卷→物理盘 extent”映射后续供 S3 源盘保护复用，避免两套自相矛盾的路径归属查询。只增最小必要 Win32 包装，不引入第三方包。

**建议分配的 subagent**：SA-IO 独占 tf_flasher.py；SA-TEST 独占 tests/test_volume_safety.py；SA-REVIEW 独立审查拒写/资源释放证据。此阶段禁止 S3–S6 编辑 tf_flasher.py。

**验收标准**：
- Given 任一目标卷 FSCTL_LOCK_VOLUME 失败 / FSCTL_DISMOUNT_VOLUME 失败，When 写入开始，Then **不得调用 PhysicalDrive WriteFile**，该目标 final 为明确失败，已持有句柄全部释放。
- Given 卷无盘符、仅目录挂载或跨多个 extent，When 解析卷，Then 正确关联所有涉及的物理盘并进入独占处理；映射错误/不完整直接拒绝。
- Given 确认无已挂载卷的空白 VHD，Then 可以按受控路径写入；不能把“枚举 API 失败”误当“空盘”。
- 单盘与多路异常相互隔离，不影响其他盘结果，取消也能释放锁。
- 测试模拟 + **仅在 S1 通过后**专用 VHD 正向 E2E，有失败/锁拒绝日志；不得以真实 TF 卡制造锁失败。

### S3 / P0：源镜像盘、工具运行盘绝不可成为目标（SPEC #3）

**步骤目标**：GUI 全选/手选和 CLI --drives 都在最终写盘准入时阻止擦写镜像源卷或程序所在卷的任一物理盘。

**可能改动的文件清单**：
- tf_flasher.py（新增最小路径→卷→磁盘解析逻辑；selectable/start/cli_flash 共用校验；借用 S2 卷映射）。
- tests/test_source_guard.py（新增：目录挂载/多 extent/UNC/解析失败/GUI CLI 路径测试）。

**参考声明**：参考 tf_flasher.py:selectable(281–289)、App.start(820–875)、cli_flash(1109–1154)，采纳既有可移除/系统盘/容量规则作为必要但不充分条件；**放弃**只凭盘号、容量和 removable 即放行。参考 S2 的卷 extent 映射，禁止回退为单纯盘符判断。

**复用声明**：路径归属复用 S2 统一卷管理器数据与现有磁盘编号，不单独开发第二套路径盘符匹配；GUI 列表可先展示不可选原因，但最终原子写入准入校验必须在 GUI/CLI 同时生效。

**建议分配的 subagent**：SA-IO 独占 tf_flasher.py；SA-TEST 独占 tests/test_source_guard.py；SA-REVIEW 只读。S2 完成交接后开始编辑。

**验收标准**：
- 源镜像在目标 U 盘上，且盘大小足够装下解压镜像：GUI 复选/全选与 CLI --drives --yes **均拒绝**；不能通过关闭 verify 绕开。
- 工具 exe 或 .py 所在可移除盘：同样拒绝；onefile 使用 sys.executable 定位运行目录而非 %TEMP% 的 __file__。
- 目录挂载点/多 extent 盘的映射包含目标时拒绝；卷映射失败/权限不足时拒绝并提示原因，不以空集合默认安全。
- 其他非源、非工具盘保持正常可选；每个写入 worker 在危险目标上 WriteFile 调用次数为 0。
- 专用 VHD 回归通过；用户真正的源盘不用作负向实写测试。

### S4 / P0：严格镜像类型门禁与完整预检前禁止写盘（SPEC #2）

**步骤目标**：任何输入在结构/内容完整性及目标容量预检成功前，都不得开始卷弹出或物理盘写入。

**可能改动的文件清单**：
- tf_flasher.py（analyze_image、App.browse/start/_run_analyzer、cli_flash、flash_one 的预检衔接及提示）。
- tests/test_image_preflight.py（新增：MBR/GPT/错误类型/空文件/异常 gzip/容量/顺序测试）。
- flasher_build/_test_write_e2e.py（修正 build_image：构造结构合法的确定性整盘测试镜像；S1 测试护栏保留）。
- flasher_build/_test_write_multi.py（仅与测试镜像结构/安全检查相关的适配，若确实无变更保持不修改）。

**参考声明**：参考 analyze_image(360–391)、_cache_load/_cache_store(394–415)、App.start(820–898)、_run_analyzer(795–817)、cli_flash(1109–1165)，保留 gzip 流式读取、SHA-256 缓存与异步 GUI 进度；**放弃**“先破坏性写入，再等待预扫描发现文件错误”的启动时序。参考现有 VHD 16MiB 伪随机 fixture，但其原始第一个扇区并非合法分区表，不能原样用于严格镜像验收。

**复用声明**：继续复用 analyze_image 的内容寻址、解压/哈希能力与元数据结构，添加最小的 MBR/GPT 解析/约束验证；不另起镜像处理管线。有效的 gzip 解压后结构校验与裸 img 走同一入口；缓存不能跳过新增结构验证或误信过期内容。

**建议分配的 subagent**：SA-IO 独占 tf_flasher.py；SA-TEST 独占三个测试文件；SA-REVIEW 负责只读审计“检查在写入之前”的调用顺序。S3 完成后才允许编辑主文件。

**验收标准**：
- .md/.txt/.zip、空文件、伪装 .gz、gzip CRC 错误/截断、无有效分区布局的垃圾内容：CLI --image 与 GUI 写盘前明确拒绝；与本轮 A 级复现对照，SPEC.md 不再被接受为完整磁盘镜像。
- 有效 MBR、有效 GPT（含保护 MBR、GPT header 和合理分区布局）以及相应 .img.gz 可通过；错误头/异常分区越界拒绝。分区镜像如需支持，**必须先有用户裁决与新增独立验收样例**。
- 压缩镜像的真实解压大小大于目标盘时，GUI 与 CLI 均 **零 WriteFile**，不再靠中途写满盘报错兜底。
- GUI 首次预检显示状态、可取消；分析失败不启动 worker/不弹出卷；成功才启动写入；不得将窗口阻塞到无响应。
- 合法 16MiB 镜像的单盘/三路专用 VHD 写入、落盘、读回 SHA-256 回归全绿；损坏输入的负向测试只用 mock/临时普通文件，不对真盘实写。
- SHA-256 对账依然可抓到预检与正式写入间源内容变化，**不得**通过放松哈希或跳过 gzip 完整性达到通过。

### S5 / P1：未扇区对齐镜像的写入字节数与 SHA-256 语义（SPEC #1）

**步骤目标**：只把原始镜像字节计入逻辑长度/哈希；填充扇区仍被正确写入，校验只读取原始镜像区间。

**可能改动的文件清单**：
- tf_flasher.py（flash_one 的尾块、n/sha、读回长度与结果字段）。
- tests/test_tail_accounting.py（新增：512B/4096B、空尾/非对齐尾、异常路径模拟）。
- flasher_build/_test_write_e2e.py（新增非对齐镜像的**受控 VHD**案例，仍受 S1 guard 限制）。

**参考声明**：参考 flash_one 529–585 的当前写入/flush/对账/读回流程；采纳 _write_all 和预期 meta SHA-256 的既有定义，**放弃** sha.update(padded_tail) 与 n += len(padded_tail)；测试对照 AGENTS.md §4 的流程约束。

**复用声明**：沿用 _RawDiskFile 写、读、seek、flush 与现有 sha256；仅拆出“logical_bytes”和“physical_written_bytes”的语义（是否实际落结果字段由测试需要决定），不另外建设抽象存储层。

**建议分配的 subagent**：SA-IO 独占 tf_flasher.py；SA-TEST 独占两个测试文件；SA-REVIEW 只读核对哈希和字节数。S4 完成后开始编辑。

**验收标准**：
- img_size 为 512k、512k+1、4096k+1（以及随机尾长度）：成功后结果逻辑 bytes == img_size，写入设备字节数向上对齐，尾部补零正确。
- 分别覆盖 512B 与 4KiB 扇区设备；预期 SHA-256 == 原始文件哈希，读回只读原始长度并匹配；原本非对齐必失败的输入在安全 VHD 上变成成功。
- 真正写入流不匹配、读回内容损坏仍失败，不得放宽比较；取消/短写/EOF 按原语义报告。
- 单盘与多路原有对齐镜像 E2E 不回退。

### S6 / P1：冻结 verify 并让台账/摘要反映实际执行（SPEC #7）

**步骤目标**：写入任务启动后 verify 不可被 UI 改写，台账和逐盘完成提示只能展示该盘确实执行的校验状态。

**可能改动的文件清单**：
- tf_flasher.py（App.__init__ 的 Checkbutton 控件引用、App.start 的 verify 快照、_set_running、_finish、flash_one 结果字段及 write_run_log 的对账语义）。
- tests/test_verify_snapshot.py（新增：开始/运行/结束状态与 per-disk 记录测试）。

**参考声明**：参考 App.__init__ 691–694、start 868–898、_set_running 905–914、_finish 988–1015、write_run_log 608–624；保留现有 UI 与日志结构兼容性，**放弃**由结束时的 mutable BooleanVar 推断过去是否做过校验。

**复用声明**：复用已有 verify bool 线程参数与 results per-drive 列表，给每盘结果增加明确的 verify_requested / verify_completed（字段命名在契约冻结后确定）而非另造全局状态中心；GUI 禁用控件只是附加防护，**结果字段才是审计真源**。

**建议分配的 subagent**：SA-IO 独占 tf_flasher.py；SA-TEST 独占 tests/test_verify_snapshot.py；SA-REVIEW 只读检查 GUI 与 JSON 台账一致。S5 完成后开始编辑。

**验收标准**：
- Given 运行前 verify=on，When 按开始，Then 运行中控件 disabled；逐盘成功且验证完成后才出现“校验通过”。
- Given verify=off，When 成功写入，Then 台账/弹窗不能出现“读回校验通过”，只可写“写入完成（未读回校验）”。
- 即使测试直接修改 BooleanVar（绕过 disabled），完成摘要和 JSON 仍依据启动快照/逐盘结果；每盘验证失败则报告失败而非成功。
- 多路执行在取消/中断时记录明确验证状态；既有日志消费者可解析新增字段，旧字段 verify 的含义不反转。
- GUI 真实点击流程与 VHD 回归都有证据；不能仅用 mock 测状态就宣布 GUI 终验通过。

### S7 / 终验：独立验收、契约回勾与逐步提交

**步骤目标**：只在 S1–S6 全部通过各自验收门后，整合最终证据，按实际修复状态更新 SPEC.md，并按步骤独立形成可审计提交。

**可能改动的文件清单**：
- SPEC.md（仅总调度改状态、补新发现、纠正 #1/#2 的旧描述）。
- plan.md（仅总调度更新步骤状态/证据和 commit SHA）。
- docs/reviews/tf_flasher_acceptance_2026-11-23.md（新增：SA-REVIEW 的只读证据报告，不复制运行产物）。

**参考声明**：参考本 plan 的一、二节契约和 AGENTS.md §7–§9、E:/WebGemini/总调度模式 .md 的验收门；采纳逐包对照、先复验再提交和每步回勾；**放弃**一次性混合所有修复成大 commit 的做法。

**复用声明**：复用已有 VHD E2E、py_compile、CLI --image/--list、GUI smoke 及各步骤独立用例；只新增聚合报告，不再建设第二套测试运行器。

**建议分配的 subagent**：SA-REVIEW **只可新增证据报告**，执行定向复验并报告测试日志/截图/失败归因；总调度独占 SPEC.md 与 plan.md，亲自裁决和统一提交。不得由子代理 git commit/push。

**验收标准**：
- 每步都单独检查：① **乱改**：实际 diff 路径 ⊆ 该步申报清单、未触碰禁改文件；② **参考对照**：能指出采纳/弃用的参考位置；③ **重复功能**：新实现无未经解释的冗余副本。任一失败退回，不签收该步。
- 每步骤独立一 commit（或事先列明的对应 commit 组），**严格 S1→S6→S7 顺序**；只能按精确路径/hunk 暂存，禁止 git add -A；子代理一律不 commit。
- 发布前 python -m py_compile 全过，单盘与多路专用 VHD E2E 全过，拒写负例全过，GUI 的实际用户可见流程经独立复验；必要时 onefile --uac-admin smoke 检查是否受影响。
- SPEC #2/#3/#4/#1/#7 只在各自验收完成后从“现行/潜伏”更新为“已修复+证据”；#5/#6/#9 未修不准误标已修，#8 维持加固建议。
- **硬件终验**：模拟/VHD 全绿不能推定真实读卡器完全健康。若需发布到真实 TF 卡，另由专门 subagent 在用户明确授权的专用测试卡上完成人工验收，并附设备型号/读卡器/扇区大小/结果，不得擅自选用户现有数据盘。
- 当前工作区既有未提交更改不得混入任何本轮 commit；无法干净隔离时暂停提交并报告用户。

## 四、依赖/并发拓扑和交付节奏

- **硬依赖**：S1 → S2 → S3 → S4 → S5 → S6 → S7。S1 的安全盘身份保护先于任何带写入的 VHD E2E。S2 的卷映射基础供 S3 复用；S4 的镜像校验改变 fixture，S5/S6 必须基于它回归。
- **有限并行**：S2 主代码与其独立单元测试可同时准备；S3 的测试案例可只读设计，但在 S2 接口冻结前不落文件；S4 测试 fixture 的专有文件可与 SA-IO 独立开发，但**同一文件在同一时刻仅有一个写者**。任何跨步 tf_flasher.py 改动都必须排队。
- **每包完成回执**：编号、前后基线、文件 diff 清单、测试命令和退出码、关键原始输出/报告路径、代码参考位置、复用说明、未验项目、是否涉及真实磁盘 I/O。缺回执即未完成。
- **验收后逐步提交**：不是“全部做完再一个提交”。若总调度暂时没收到独立验收证据，当前步骤保持 pending，下一步源码修改不得越过同文件所有权边界。
- **失联**：先只读盘点工作区现状和任务回执；唤醒原代理；失败后把已完成/未完成文件清单发给续作者。不要重复写入同一盘，也不要用重启或重跑破坏现场。
- **回滚**：每步骤变更可按其单独 commit 回退；若回滚影响后续步骤，先在契约中列依赖，再按反向顺序处置。

## 五、非本轮严重修复范围（保留但不假装已解决）

| 待办 | 优先级 | 后续最小方向 | 前置验收 |
|---|---|---|---|
| SPEC #6 台账写失败 | P2 | write_run_log 调用方隔离 I/O 异常，GUI 保留完成弹窗，CLI 保留真实写入结果与明确日志告警；不得报告“日志已保存” | 模拟 LOG_DIR 无权限/只读，保证 UI 不被异常吞掉 |
| SPEC #5 frozen 提权 | P2 | relaunch_elevated 区分 frozen/unfrozen，检查 ShellExecuteW 结果；避免把 -I 传给 exe | 专门构建无 --uac-admin 的 onefile 测试，保留默认 manifest 构建 |
| SPEC #9 BAT 透传/CLI 进度 | P3 | BAT %*；校验阶段独立进度基线及 total=0 保护 | BAT --list 打开 CLI；写/校验均能展示合理进度 |
| SPEC #8 worker 最外层 final 兜底 | P3 | 如未来重构 worker 执行框架，再增强异常终结信号 | 先证明有真实触发路径，不为了“防御”改坏事件语义 |

这些条目应在下一轮用**独立的新 plan 工作包**实施，不得让 S1–S7 子代理顺手修，避免扩大本轮物理盘安全修复的 blast radius。

## 六、当前完成度和未决裁决

- [x] 已读取总调度模式与项目 AGENTS.md、SPEC.md。
- [x] 已按当前 tf_flasher.py 逐条核对 SPEC #1–#9 代码调用链。
- [x] 已安全复现 #2 对非镜像文件的错误接受：python tf_flasher.py --image SPEC.md，返回压缩/解压均 6183 B。
- [x] 已执行 python -m py_compile tf_flasher.py flasher_build/_test_write_e2e.py flasher_build/_test_write_multi.py，通过。
- [x] 已发现独立于 SPEC 的 VHD 测试脚本盘号越权风险，并安排 S1 优先封堵。
- [ ] 用户裁决“是否支持非完整磁盘镜像”及合法输入边界；未决时按仅完整磁盘镜像门禁执行。
- [ ] 总调度建立可隔离的 Git 基线/工作树，不混入当前用户未提交文件。
- [ ] S1–S7 尚未派发、尚未编写修复、尚无 VHD/真卡写入验证证据。

**完成定义**：只有通过“安全拒写 + 真实 VHD 写入校验 + 实际 GUI/CLI 结果一致 + 逐步独立审查 + 合规 Git 提交”的问题，才能在 SPEC.md 标为已修复。