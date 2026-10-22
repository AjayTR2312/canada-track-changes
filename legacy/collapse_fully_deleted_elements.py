import re


def collapse_empty_para_with_trace_deleted(xml_content: str) -> str:
    """
    Collapse empty <para> elements that contain only <trace.deleted /> → <trace.deleted/>
    
    Pattern matches:
        <para date.updated="0">
            <para.text date.updated="0"><trace.deleted /></para.text>
        </para>
    
    And replaces with:
        <trace.deleted/>
    """

    xml_content = xml_content.replace('</trace.deleted/>', '<trace.deleted/>')
    # Use \s* to handle Windows \r\n line endings
    pattern = r'<para date\.updated="0">\s*<para\.text date\.updated="0"><trace\.deleted\s*/></para\.text>\s*</para>'
    matches = re.findall(pattern, xml_content)
    print(f"  [COLLAPSE] Found {len(matches)} empty <para> with <trace.deleted /> to collapse.")
    result = re.sub(pattern, r'<trace.deleted/>', xml_content)

    # Collapse empty <name.block> with only <trace.deleted /> in designator → <trace.deleted/>
    pattern2 = r'<name\.block toc\.include="y" date\.updated="0">\s*<designator date\.updated="0"><trace\.deleted\s*/></designator>\s*</name\.block>'
    matches2 = re.findall(pattern2, result)
    print(f"  [COLLAPSE] Found {len(matches2)} empty <name.block> with <trace.deleted /> to collapse.")
    result = re.sub(pattern2, r'<trace.deleted/>', result)
    
    return result


# pxmlpath=r"C:\projectdata\xmlproject\new_approach_working\testing_exe_ouputs\testing_april2\Test_5 (April 2, 2026)\CDN_KN_EGWSI Ch 5\exe_testing_ajay6\save_afterfootnotes_insertions.pxml"
# with open(pxmlpath, 'r', encoding='utf-8') as f:
#     updated_pxmlcontent = f.read()
# updated_pxmlcontent = collapse_empty_para_with_trace_deleted(updated_pxmlcontent)
# with open(r"C:\projectdata\xmlproject\new_approach_working\testing_exe_ouputs\testing_april2\Test_5 (April 2, 2026)\updated_pxmlcontent_after_postprocessing.pxml", 'w', encoding='utf-8') as f:
#     f.write(updated_pxmlcontent)
