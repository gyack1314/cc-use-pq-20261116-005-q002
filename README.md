# 气候指标批次采集台账

本项目供研究团队离线接收不同机构交付的气候指标摘录。系统核验来源和指标契约，保留每次批次修订，记录质量证据，并发布不可变、可校验的快照。演示数据均为本地合成数据，不代表真实气候观测，也不在运行时访问外部网站。

使用 Python 3.11 或更新版本即可运行，无第三方依赖。以下命令均从项目根目录执行。

## 快速演示

运行 python -m climateledger demo --workspace fixtures，完成本地演示：创建 SQLite 数据库、注册合成指标与来源、摄入两行数据并发布、对 2026-02 执行一次区间撤回（生成纯撤回发布）、补交更正并再次发布，最后校验最新与历史导出。再次执行会重用相同批次、通知和发布版本。

单独使用时，把 --db path/to/ledger.sqlite3 放在子命令前。依次执行 register-indicator contract.json、register-source contract.json；需要单位换算时执行 register-unit rule.json。用 ingest batch.manifest.json 摄入数据，batch ID 查看记录；待审批次用 approve ID REVISION 审核，然后用 publish ID REVISION 发布。

export VERSION --output bundle.json 导出指定快照，verify bundle.json 检查摘要，series INDICATOR --version VERSION 查询发布值。coverage、discrepancies、compare、replay 和 audit 分别用于查看覆盖、来源分歧、版本变化、原始文件重放及审计链。

## 撤回与更正

撤回通知是本地 JSON 文件，包含 source、notice_id、indicator、first_period、last_period、reason 和可选 detail，针对单一来源与指标的连续期间。retract notice.json 记录通知：内容摘要相同则重复提交幂等，不产生新代次；内容变化在同一 notice_id 下形成新代次，旧代次保留但仅最新代次生效。通知一经记录即对后续发布生效：apply-retraction ID REVISION 立即生成只含撤回的发布，普通批次发布也会携带当时未应用的撤回。发布清单的 retractions 字段列出本次应用的撤回及移除条目数；移除只针对该来源该指标在区间内的已发布值，其他月份和其他来源的条目不受影响。已发布旧版本保持不可变，export 与 verify 始终可核验。

更正尚未到齐时，当前发布版本中该区间没有值；retractions 命令按通知列出状态（open 已记录、applied 已生效、fulfilled 已补全）与未补交期间，coverage 报告附带未完结撤回的缺口，旧版本中的原值仍可查询。更正按普通批次摄入：质量检查对触及未完结撤回区间的批次给出 retraction_pending 审核发现，逐个列出尚未补交的期间；区间在当前发布中补全时通知转为 fulfilled。重复 apply-retraction 返回首次应用它的发布，重复到达的相同通知不会产生新代次。

## 输入与状态

指标契约包含 key、title、canonical_unit 和 cadence（月份或年份）；可附带上下限、精度和负值许可。来源契约包含标识、标题、授权说明及允许提交的指标列表。相同标识下契约不可静默改写。单位规则采用“原值乘以 factor 加 offset”换算为规范单位。

批次清单指定来源、外部批次编号、同目录的 CSV 或 JSON 数据文件、文件 SHA-256、行数及四个字段的列映射：indicator、period、value、unit。文件仅从本地读取。非法行形成拒收证据；序列内部缺期和已发布值的变更需人工审核，不会被悄悄修正。相同字节重复提交幂等，内容变化形成修订。清单中的行数和摘要可检测不完整或被修改的输入。

发布版本包含上个版本的摘要、来源文件摘要、批次修订、当前测量集合及导出摘要。不同来源对同一指标和期间的观测保持独立。旧版本始终可以独立导出和验证。

## 验证命令

python -m unittest discover -s tests -v

python -m compileall -q climateledger

python -m climateledger demo --workspace fixtures
