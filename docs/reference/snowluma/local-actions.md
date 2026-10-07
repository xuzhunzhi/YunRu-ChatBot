# 本机 SnowLuma 动作清单

从本机安装的分包里抽取出来的动作清单。**只读**抽取，没有改动那份安装里的任何字节。

- 抽取到 **198** 个动作。
- 分包里有 201 条动作声明，其中
  3 条是包装函数的定义、没有字面量动作名，不计入。
- 来源文件：`C:\ProgramData\NapCatQQ Desktop\components\SnowLuma\config-ClrCrmAk.js`
- 抽取日期：2026-10-08
- 抽取方式：本目录 `extract-actions.py`；复跑方法见 `README.md`。

## 动作总表

| 动作 | 分类 | 别名 | 参数 |
| --- | --- | --- | --- |
| `can_send_image` | 信息 | - | - |
| `can_send_record` | 信息 | - | - |
| `get_login_info` | 信息 | - | - |
| `get_status` | 信息 | - | - |
| `get_version_info` | 信息 | - | - |
| `delete_friend` | 好友 | - | `user_id`, `block` |
| `get_friend_list` | 好友 | - | - |
| `get_stranger_info` | 好友 | - | `user_id` |
| `.get_word_slices` | 扩展 | - | - |
| `_del_group_notice` | 扩展 | - | `group_id`, `fid`, `notice_id` |
| `_get_friend_dress` | 扩展 | - | `user_id` |
| `_get_group_notice` | 扩展 | - | `group_id` |
| `_get_model_show` | 扩展 | - | `model` |
| `_mark_all_as_read` | 扩展 | - | - |
| `_send_group_notice` | 扩展 | - | `group_id`, `content`, `image`, `pinned`, `type`, `send_to_new_members`, `is_show_edit_card`, `tip_window_type`, `confirm_required` |
| `_set_model_show` | 扩展 | - | - |
| `add_custom_face` | 扩展 | - | `file` |
| `bot_exit` | 扩展 | - | - |
| `cancel_group_todo` | 扩展 | - | `group_id`, `message_id` |
| `check_url_safely` | 扩展 | - | - |
| `clean_cache` | 扩展 | - | - |
| `click_inline_keyboard_button` | 扩展 | - | `group_id`, `bot_appid`, `msg_seq`, `button_id`, `callback_data` |
| `complete_group_todo` | 扩展 | - | `group_id`, `message_id` |
| `create_collection` | 扩展 | - | - |
| `create_flash_task` | 扩展 | - | `files`, `name`, `thumb_path` |
| `delete_custom_face` | 扩展 | - | `emoji_id` |
| `delete_essence_msg` | 扩展 | - | `message_id` |
| `delete_flash_file` | 扩展 | - | `fileset_id` |
| `delete_group_folder` | 扩展 | - | `group_id`, `folder_id` |
| `download_file` | 扩展 | - | `url`, `base64`, `name`, `headers` |
| `download_fileset` | 扩展 | - | `fileset_id`, `file_name`, `file_index` |
| `fetch_custom_face` | 扩展 | - | `count`, `return_type` |
| `fetch_custom_face_detail` | 扩展 | - | `count` |
| `fetch_emoji_like` | 扩展 | - | `message_id`, `emojiId`, `count`, `cookie` |
| `fetch_ptt_text` | 扩展 | `get_ptt_text`, `get_record_text` | `message_id` |
| `forward_friend_single_msg` | 扩展 | - | `message_id`, `user_id` |
| `forward_group_single_msg` | 扩展 | - | `message_id`, `group_id` |
| `friend_poke` | 扩展 | - | `user_id`, `target_id` |
| `get_ai_characters` | 扩展 | - | `group_id`, `chat_type` |
| `get_ai_record` | 扩展 | - | `group_id`, `character`, `text`, `chat_type` |
| `get_clientkey` | 扩展 | - | - |
| `get_collection_list` | 扩展 | - | `category`, `count` |
| `get_cookies` | 扩展 | - | `domain` |
| `get_credentials` | 扩展 | - | `domain` |
| `get_csrf_token` | 扩展 | - | - |
| `get_doubt_friends_add_request` | 扩展 | - | `count` |
| `get_emoji_likes` | 扩展 | - | `message_id`, `emoji_id` |
| `get_essence_msg_list` | 扩展 | - | `group_id` |
| `get_file` | 扩展 | - | `file_id`, `file` |
| `get_fileset_id` | 扩展 | - | `share_code` |
| `get_fileset_info` | 扩展 | - | `fileset_id` |
| `get_flash_file_list` | 扩展 | - | `fileset_id` |
| `get_flash_file_url` | 扩展 | - | `fileset_id`, `file_name`, `file_index` |
| `get_forward_msg` | 扩展 | - | `id`, `message_id` |
| `get_friend_msg_history` | 扩展 | - | `user_id`, `message_id`, `count`, `reverse_order` |
| `get_friends_with_category` | 扩展 | - | - |
| `get_group_at_all_remain` | 扩展 | - | `group_id` |
| `get_group_detail_info` | 扩展 | - | `group_id`, `no_cache` |
| `get_group_file_system_info` | 扩展 | - | `group_id` |
| `get_group_ignore_add_request` | 扩展 | - | - |
| `get_group_ignored_notifies` | 扩展 | - | - |
| `get_group_info_ex` | 扩展 | - | `group_id`, `no_cache` |
| `get_group_msg_history` | 扩展 | - | `group_id`, `message_id`, `count`, `reverse_order` |
| `get_group_shut_list` | 扩展 | - | `group_id` |
| `get_group_signed_list` | 扩展 | - | `group_id` |
| `get_group_todo_list` | 扩展 | - | `group_id` |
| `get_image` | 扩展 | - | `file`, `file_id` |
| `get_mini_app_ark` | 扩展 | - | `type`, `title`, `desc`, `picUrl`, `pic_url`, `jumpUrl`, `jump_url` |
| `get_msg_emoji_likes` | 扩展 | - | `message_id` |
| `get_online_clients` | 扩展 | - | `no_cache` |
| `get_profile_like` | 扩展 | - | `user_id`, `start`, `count` |
| `get_recent_contact` | 扩展 | - | `count` |
| `get_record` | 扩展 | - | `file`, `file_id`, `out_format` |
| `get_rkey` | 扩展 | `nc_get_rkey` | - |
| `get_rkey_server` | 扩展 | - | - |
| `get_share_link` | 扩展 | - | `fileset_id` |
| `get_unidirectional_friend_list` | 扩展 | - | - |
| `group_poke` | 扩展 | - | `group_id`, `user_id` |
| `list_filesets` | 扩展 | - | - |
| `mark_group_msg_as_read` | 扩展 | - | `message_id`, `group_id` |
| `mark_msg_as_read` | 扩展 | - | `message_id`, `target_id` |
| `mark_private_msg_as_read` | 扩展 | - | `message_id`, `user_id` |
| `modify_custom_face` | 扩展 | - | `emoji_id`, `desc` |
| `move_custom_face_to_front` | 扩展 | - | `emoji_id` |
| `nc_get_packet_status` | 扩展 | - | - |
| `nc_get_user_status` | 扩展 | - | `user_id` |
| `ocr_image` | 扩展 | `.ocr_image` | `image` |
| `rename_flash_file` | 扩展 | - | `fileset_id`, `new_name` |
| `request_decrypt_key` | 扩展 | - | `db_path` |
| `send_ark_share` | 扩展 | - | `user_id`, `group_id`, `phone_number` |
| `send_flash_msg` | 扩展 | - | `fileset_id`, `user_id`, `group_id` |
| `send_forward_msg` | 扩展 | - | `messages`, `message`, `message_type`, `group_id`, `user_id` |
| `send_group_ai_record` | 扩展 | - | `group_id`, `character`, `text`, `chat_type` |
| `send_group_ark_share` | 扩展 | - | `group_id` |
| `send_group_forward_msg` | 扩展 | - | `group_id`, `messages`, `message` |
| `send_like` | 扩展 | - | `user_id`, `times` |
| `send_packet` | 扩展 | `.send_packet` | `cmd`, `data`, `rsp` |
| `send_poke` | 扩展 | - | `user_id`, `group_id` |
| `send_private_forward_msg` | 扩展 | - | `user_id`, `messages`, `message` |
| `send_tuwen_ark` | 扩展 | - | `user_id`, `group_id`, `title`, `desc`, `summary`, `preview_url`, `jump_url` |
| `set_diy_online_status` | 扩展 | - | `face_id`, `face_type`, `wording` |
| `set_doubt_friends_add_request` | 扩展 | - | `flag`, `approve` |
| `set_essence_msg` | 扩展 | - | `message_id` |
| `set_friend_remark` | 扩展 | - | `user_id`, `remark` |
| `set_friends_category` | 扩展 | - | `uin`, `categoryId`, `categoryName` |
| `set_group_reaction` | 扩展 | - | `group_id`, `message_id`, `code`, `is_set` |
| `set_group_remark` | 扩展 | - | `group_id`, `remark` |
| `set_group_robot_add_option` | 扩展 | - | `group_id`, `robot_member_switch`, `robot_member_examine` |
| `set_group_sign` | 扩展 | `send_group_sign` | `group_id` |
| `set_group_todo` | 扩展 | - | `group_id`, `message_id` |
| `set_input_status` | 扩展 | - | `user_id`, `event_type` |
| `set_msg_emoji_like` | 扩展 | - | `message_id`, `emoji_id`, `set` |
| `set_online_status` | 扩展 | - | `status`, `ext_status`, `battery_status` |
| `set_qq_avatar` | 扩展 | - | `file` |
| `set_qq_profile` | 扩展 | - | `nickname`, `personal_note`, `sex` |
| `set_restart` | 扩展 | - | - |
| `set_self_longnick` | 扩展 | - | `longNick`, `long_nick` |
| `share_group_ex` | 扩展 | - | `group_id` |
| `share_peer` | 扩展 | - | `user_id`, `group_id`, `phone_number` |
| `trans_group_file` | 扩展 | - | `group_id`, `file_id` |
| `translate_en2zh` | 扩展 | - | `words` |
| `upload_forward_msg` | 扩展 | - | `messages`, `message`, `group_id` |
| `upload_foward_msg` | 扩展 | - | `messages`, `message`, `group_id` |
| `clean_stream_temp_file` | 流式接口 | - | - |
| `download_file_image_stream` | 流式接口 | - | - |
| `download_file_record_stream` | 流式接口 | - | - |
| `download_file_stream` | 流式接口 | - | - |
| `test_download_stream` | 流式接口 | - | `error` |
| `upload_file_stream` | 流式接口 | - | `stream_id`, `chunk_data`, `chunk_index`, `total_chunks`, `file_size`, `expected_sha256`, `is_complete`, `filename`, `reset`, `verify_only`, `file_retention` |
| `delete_msg` | 消息 | - | `message_id` |
| `get_msg` | 消息 | - | `message_id` |
| `send_custom_face` | 消息 | - | `emoji_id`, `group_id`, `user_id`, `reply_to` |
| `send_group_msg` | 消息 | - | `group_id`, `message`, `auto_escape` |
| `send_msg` | 消息 | - | `message`, `message_type`, `group_id`, `user_id`, `auto_escape` |
| `send_private_msg` | 消息 | - | `user_id`, `message`, `group_id`, `auto_escape` |
| `comment_qzone` | 空间 | - | `tid`, `content`, `target_uin`, `images` |
| `delete_qzone_msg` | 空间 | - | `tid` |
| `get_qzone_feeds` | 空间 | - | `page_num`, `count` |
| `get_qzone_msg_list` | 空间 | - | `target_uin`, `pos`, `num` |
| `like_qzone` | 空间 | - | `tid`, `target_uin`, `abstime` |
| `send_qzone_msg` | 空间 | - | `content`, `images`, `ugc_right`, `target_uins` |
| `set_qzone_ban` | 空间 | - | `user_id`, `enable` |
| `set_qzone_msg_right` | 空间 | - | `tid`, `ugc_right`, `target_uins` |
| `unlike_qzone` | 空间 | - | `tid`, `target_uin`, `abstime` |
| `fetch_face_entity` | 系统表情 | - | `face_id`, `refresh` |
| `fetch_super_face_id` | 系统表情 | - | `face_id`, `refresh` |
| `fetch_sys_faces` | 系统表情 | - | `refresh` |
| `search_sys_faces` | 系统表情 | - | `query` |
| `get_group_honor_info` | 群信息 | - | `group_id`, `type` |
| `get_group_info` | 群信息 | - | `group_id`, `no_cache` |
| `get_group_list` | 群信息 | - | `no_cache` |
| `get_group_member_info` | 群信息 | - | `group_id`, `user_id`, `no_cache` |
| `get_group_member_list` | 群信息 | - | `group_id`, `no_cache` |
| `get_group_system_msg` | 群信息 | - | `group_id`, `only_pending`, `count` |
| `create_group_file_folder` | 群文件 | - | `group_id`, `name`, `parent_id` |
| `delete_group_file` | 群文件 | - | `group_id`, `file_id` |
| `delete_group_file_folder` | 群文件 | - | `group_id`, `folder_id` |
| `get_group_file_url` | 群文件 | - | `group_id`, `file_id`, `busid` |
| `get_group_files_by_folder` | 群文件 | - | `group_id`, `folder_id`, `folder` |
| `get_group_root_files` | 群文件 | - | `group_id` |
| `get_private_file_url` | 群文件 | - | `user_id`, `file_id`, `file_hash` |
| `move_group_file` | 群文件 | - | `group_id`, `file_id`, `parent_directory`, `target_directory` |
| `rename_group_file` | 群文件 | - | `group_id`, `file_id`, `current_parent_directory`, `new_name` |
| `rename_group_file_folder` | 群文件 | - | `group_id`, `folder_id`, `new_folder_name`, `name` |
| `upload_group_file` | 群文件 | - | `group_id`, `file`, `name`, `folder`, `folder_id`, `upload_file` |
| `upload_private_file` | 群文件 | - | `user_id`, `file`, `name`, `upload_file` |
| `cancel_group_album_media_like` | 群相册 | - | `group_id`, `album_id`, `batch_id`, `lloc` |
| `del_group_album_media` | 群相册 | - | `group_id`, `album_id`, `lloc` |
| `do_group_album_comment` | 群相册 | - | `group_id`, `album_id`, `lloc`, `content` |
| `get_group_album_list` | 群相册 | - | `group_id` |
| `get_group_album_media_list` | 群相册 | - | `group_id`, `album_id`, `attach_info` |
| `get_qun_album_list` | 群相册 | - | `group_id`, `attach_info` |
| `set_group_album_media_like` | 群相册 | - | `group_id`, `album_id`, `batch_id`, `lloc` |
| `upload_image_to_qun_album` | 群相册 | - | `group_id`, `album_id`, `album_name`, `file` |
| `upload_video_to_qun_album` | 群相册 | - | `group_id`, `album_id`, `album_name`, `file` |
| `add_group_tag` | 群管理 | - | `group_class`, `name` |
| `delete_group_tag` | 群管理 | - | `group_class`, `name` |
| `get_group_admin_settings` | 群管理 | - | `group_id` |
| `get_group_tags` | 群管理 | - | `group_class` |
| `set_group_add_option` | 群管理 | - | `group_id`, `add_type`, `group_question`, `group_answer` |
| `set_group_admin` | 群管理 | - | `group_id`, `user_id`, `enable` |
| `set_group_anonymous` | 群管理 | - | - |
| `set_group_anonymous_ban` | 群管理 | - | - |
| `set_group_ban` | 群管理 | - | `group_id`, `user_id`, `duration` |
| `set_group_card` | 群管理 | - | `group_id`, `user_id`, `card` |
| `set_group_kick` | 群管理 | - | `group_id`, `user_id`, `reject_add_request` |
| `set_group_kick_members` | 群管理 | - | `group_id`, `user_id`, `reject_add_request` |
| `set_group_leave` | 群管理 | - | `group_id` |
| `set_group_member_invite_policy` | 群管理 | - | `group_id`, `policy` |
| `set_group_member_permissions` | 群管理 | - | `group_id`, `allow_member_upload_album`, `allow_member_temporary_session`, `allow_member_create_group` |
| `set_group_name` | 群管理 | - | `group_id`, `group_name` |
| `set_group_new_member_history_visibility` | 群管理 | - | `group_id`, `visible` |
| `set_group_portrait` | 群管理 | - | `group_id`, `file` |
| `set_group_search` | 群管理 | - | `group_id`, `no_finger_open`, `no_code_finger_open` |
| `set_group_special_title` | 群管理 | - | `group_id`, `user_id`, `special_title` |
| `set_group_whole_ban` | 群管理 | - | `group_id`, `enable` |
| `set_friend_add_request` | 请求 | - | `flag`, `approve` |
| `set_group_add_request` | 请求 | - | `flag`, `sub_type`, `type`, `approve`, `reason` |

## 按分类计数

| 分类 | 动作数 |
| --- | --- |
| 信息 | 5 |
| 好友 | 3 |
| 扩展 | 115 |
| 流式接口 | 6 |
| 消息 | 6 |
| 空间 | 9 |
| 系统表情 | 4 |
| 群信息 | 6 |
| 群文件 | 12 |
| 群相册 | 9 |
| 群管理 | 21 |
| 请求 | 2 |

## 抽取时的判定规则（抽的时候实际用到的，不是猜的）

- 安装目录里的 `index.mjs` 是打包产物，动作注册表被拆成分包放在 `config-<hash>.js` 里；
  本机当前是 `config-ClrCrmAk.js`。
- 每个动作都由四个声明辅助函数之一写出来，而它们最终都走 `defineAction`：

  | 声明写法 | 含义 | 本机条数 |
  | --- | --- | --- |
  | `defineAction({{...}})` | 基础写法 | 131 |
  | `defineStreamAction({{...}})` | 基础 + `stream: true` | 5 |
  | `groupAction({{...}})` | 预置必填 `group_id` | 55 |
  | `groupUserAction({{...}})` | 预置必填 `group_id` + `user_id` | 7 |

- `name` 属性有两种写法：普通字符串，或数组（`[0]` 是动作名，其余是别名）。
  本机共 5 个动作带别名，都在上面那张表里。
- 分类来自同一分包里的 `ACTION_GROUPS` 字面量：它把分类名映射到一个或多个
  `actions$N` 数组，每条声明算在包含它的**最内层**数组所属的分类里。
- `.get_word_slices`、`.ocr_image`、`.send_packet` 这些带点号的名字是真实动作或别名，
  不是内部函数；真正被排除的只有 `ACTION_REGISTRY` 里那两个快速操作处理器
  （`.handle_quick_operation` / `.handle_quick_operation_async`）——它们不在
  `ACTION_GROUPS` 里、也不是可调用的 OneBot 动作。

## 参数列说明

参数名是从同一份分包里每个动作 `params: {...}` 字面量的顶层键抽出来的，按原始书写顺序排列。
用 `groupAction` / `groupUserAction` 写出来的动作，其 `group_id` / `user_id` 由辅助函数预置、
不在动作自己的字面量里，上表已把它们补回。

**参数列没有类型、必填性和默认值**：那三样在分包里是链式调用构造出来的
（`f.string({ allowEmpty: false })` 这种），静态抽取不能保证忠实，所以这里不写、不猜。
要类型、默认值和应答形状，请看官方文档那一份（`api/` 目录下）。
