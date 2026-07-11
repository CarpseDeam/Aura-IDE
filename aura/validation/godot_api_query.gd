extends SceneTree

const ApiIntrospection = preload("../godot_editor/addon/perception/api_introspection.gd")
const OUTPUT_PREFIX := "AURA_GODOT_API_JSON:"


func _init() -> void:
	var args := OS.get_cmdline_user_args()
	if args.size() != 1:
		printerr("Godot API query requires one JSON argument")
		quit(2)
		return
	var params: Variant = JSON.parse_string(args[0])
	if not params is Dictionary:
		printerr("Godot API query argument is not a JSON object")
		quit(2)
		return
	var response := ApiIntrospection.new().describe(params)
	print(OUTPUT_PREFIX + JSON.stringify(response))
	quit(0 if response.get("ok", false) else 1)
